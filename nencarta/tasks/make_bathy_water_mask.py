import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from curve2flood import Curve2Flood_MainFunction, remove_cells_not_connected
from numba import njit
from osgeo import gdal

from nencarta.core.enumerations import Mapper
from nencarta.core.raster import Raster
from nencarta.core.vector import Vector
from nencarta.logger import LOG
from nencarta.tasks.channel_detection import (
    ChannelSettings,
    _feature_distance,
    flood_patches,
    nearest_cell,
    river_channel,
)
from nencarta.tasks.configs import _write_config
from nencarta.tasks.stream_conflation import cell_sizes_m
from nencarta.workspace import Workspace

# Land cover's water this far above the river's level on the DEM isn't the river (see near_the_rivers_level)
WATER_MAX_RISE_M = 1.0
# A reach more than this share of whose stream cells have no water beside them in the mask is 1 cell wide all along
# (see one_cell_wide_where_mostly)
NARROW_SHARE = 0.5
# Rows of the grid worked on at a time where every cell's nearest stream cell is used, so the temporary arrays stay small
_CHUNK_ROWS = 512


def reach_areas_km2(streams_gdf: gpd.GeoDataFrame | pd.DataFrame, configs) -> dict:
    """Each reach's drainage area in km2, by id, from area_km2_field or else area_m2_field (empty without either)."""
    id_col = configs.stream_id_field
    if streams_gdf is None or id_col not in streams_gdf.columns:
        return {}
    if configs.area_km2_field and configs.area_km2_field in streams_gdf.columns:
        area = pd.to_numeric(streams_gdf[configs.area_km2_field], errors="coerce")
    elif configs.area_m2_field and configs.area_m2_field in streams_gdf.columns:
        area = pd.to_numeric(streams_gdf[configs.area_m2_field], errors="coerce") / 1e6
    else:
        return {}
    ids = pd.to_numeric(streams_gdf[id_col], errors="coerce")
    keep = ids.notna() & area.notna()
    return dict(zip(ids[keep].astype(np.int64).tolist(), area[keep].astype(float).tolist()))


def dem_channel(dem_raster: Raster | gdal.Dataset, dem: np.ndarray, valid: np.ndarray, streams: np.ndarray, area_km2: dict,
                connected: bool = True, flat_water: bool = True, channel: bool = True,
                flattens_rivers: bool | None = None):
    """
    The channel the DEM shows along ``streams`` (see channel_detection.river_channel, which skips ``flat_water`` or
    ``channel`` when told to), on the grid of ``dem_raster`` (a Raster, or a gdal.Dataset). ``flattens_rivers``
    says whether the DEM flattens every river wide enough to show (None: its flat water says, see
    channel_detection.flattens_its_rivers).
    """
    if isinstance(dem_raster, gdal.Dataset):
        geotransform, projection = dem_raster.GetGeoTransform(), dem_raster.GetProjection()
    else:
        geotransform, projection = dem_raster.geotransform, dem_raster.projection

    dx, dy = cell_sizes_m(geotransform, projection, dem.shape[0])
    return river_channel(dem, valid, streams, area_km2, float(np.median(dx)), dy,
                         ChannelSettings(flattens_rivers=flattens_rivers), connected=connected,
                         flat_water=flat_water, channel=channel)


def near_the_rivers_level(dem: np.ndarray, streams: np.ndarray, max_rise: float = WATER_MAX_RISE_M,
                          cells: np.ndarray | None = None) -> np.ndarray:
    """
    The cells of ``dem`` (before the burn) no more than ``max_rise`` above the river's level beside them, where the
    DEM has the river's water. ``streams`` is the stream raster: each stream cell's reach id (> 0), or True.

    The river's level beside a cell is the lowest of the stream cells within twice as far as the nearest one and 5
    cells more, each carried up its reach's slope by how far it lies along the river from the nearest one. Not just
    the nearest one's level: a tributary's line can run across a dry bar beside the river. And not the lowest one's
    as it is: down a sloping river the water beside a cell is higher than the river farther down. Land cover's water
    higher than that is ground the DEM shows dry, a bar or a floodplain the imagery caught flooded, and ARC, whose
    banks are where the water mask's water ends, would carve it down to the river's bed.

    Only ``cells`` (default: all) are tested; the rest are False.
    """
    streams = np.asarray(streams)
    on_stream = streams > 0
    if not on_stream.any():
        return np.zeros(dem.shape, bool)
    nearest = nearest_cell(on_stream)
    # The level is never above the nearest stream cell's, so only cells within max_rise of that can pass. Found a
    # block of rows at a time, with their distance to the nearest stream cell, to keep the temporary arrays small.
    ncols = dem.shape[1]
    found, distances = [], []
    for r0 in range(0, dem.shape[0], _CHUNK_ROWS):
        r1 = min(r0 + _CHUNK_ROWS, dem.shape[0])
        rows, cols = np.nonzero(np.ones((r1 - r0, ncols), bool) if cells is None else cells[r0:r1])
        rows += r0
        near_rows, near_cols = np.divmod(nearest[rows, cols], ncols)
        keep = dem[rows, cols] <= dem[near_rows, near_cols] + max_rise
        rows, cols = rows[keep], cols[keep]
        found.append(rows * ncols + cols)
        distances.append(_feature_distance(near_rows[keep] - rows, near_cols[keep] - cols, (1.0, 1.0)))
    del nearest
    candidates, distance = np.concatenate(found), np.concatenate(distances)

    rows, cols = np.nonzero(on_stream)
    slope = _reach_slopes(dem[rows, cols].astype(np.float64), streams[rows, cols].astype(np.int64))
    block = 16
    n_block_cols = -(-dem.shape[1] // block)
    bucket = (rows // block) * n_block_cols + cols // block
    order = np.argsort(bucket, kind="mergesort")
    rows, cols, bucket, slope = rows[order], cols[order], bucket[order], slope[order]
    count = np.bincount(bucket, minlength=-(-dem.shape[0] // block) * n_block_cols)
    first = np.concatenate(([0], np.cumsum(count)[:-1]))
    return _near_level(dem.shape[0], ncols, candidates, distance, dem.ravel()[candidates].astype(np.float64), rows,
                       cols, dem[rows, cols].astype(np.float64), slope, first, count, block, n_block_cols, max_rise)


def _reach_slopes(z: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """
    For each stream cell (height ``z``, reach ``ids``), how much its reach falls per cell along it: the spread
    between the 10th and 90th percentiles of its cells' heights over its number of cells.
    """
    order = np.argsort(ids, kind="mergesort")
    bounds = np.flatnonzero(np.diff(ids[order])) + 1
    slope = np.empty(z.size, np.float64)
    for part in np.split(order, bounds):
        heights = z[part]
        slope[part] = max(float(np.percentile(heights, 90) - np.percentile(heights, 10)), 0.0) / part.size
    return slope


@njit(cache=True)
def _near_level(nrows: int, ncols: int, candidates: np.ndarray, distance: np.ndarray, height: np.ndarray,
                rows: np.ndarray, cols: np.ndarray, z: np.ndarray, slope: np.ndarray, first: np.ndarray,
                count: np.ndarray, block: int, n_block_cols: int, max_rise: float) -> np.ndarray:
    """
    near_the_rivers_level for ``candidates`` (flat indices on an ``nrows`` by ``ncols`` grid, each ``distance`` cells
    from the nearest stream cell and ``height`` high), with the stream cells bucketed in ``block``-cell squares.

    A stream cell is never lower than its own height, so a square whose lowest stream cell isn't ``max_rise`` below a
    candidate can't hold a lower level: the squares are searched from big (4 x 4 blocks, 16 x 16, ...) to small,
    skipping those, and those beyond the search's reach. Water far from any stream, a lake's, searches far.
    """
    n_block_rows = (nrows + block - 1) // block
    # The lowest stream cell of each block, and of each square of 4 x 4 of those, up to one square for the grid
    lowest = [np.full((n_block_rows, n_block_cols), np.inf)]
    for b in range(n_block_rows * n_block_cols):
        for i in range(first[b], first[b] + count[b]):
            lowest[0][b // n_block_cols, b % n_block_cols] = min(lowest[0][b // n_block_cols, b % n_block_cols], z[i])

    while lowest[-1].shape[0] > 1 or lowest[-1].shape[1] > 1:
        below = lowest[-1]
        above = np.full(((below.shape[0] + 3) // 4, (below.shape[1] + 3) // 4), np.inf)
        for br in range(below.shape[0]):
            for bc in range(below.shape[1]):
                above[br // 4, bc // 4] = min(above[br // 4, bc // 4], below[br, bc])
        lowest.append(above)

    stack_level = np.empty(16 * len(lowest) + 16, np.int64)
    stack_row = np.empty_like(stack_level)
    stack_col = np.empty_like(stack_level)
    out = np.zeros((nrows, ncols), np.bool_)
    for k in range(candidates.size):
        r = candidates[k] // ncols
        c = candidates[k] % ncols
        d = distance[k]
        reach = 2.0 * d + 5.0
        lowest_allowed = height[k] - max_rise
        # Start from the squares at least as big as the reach, at most 3 x 3 of them
        level, size = 0, block
        while level < len(lowest) - 1 and size < reach:
            level += 1
            size *= 4

        top = 0
        for br in range(max(int((r - reach) // size), 0), min(int((r + reach) // size), lowest[level].shape[0] - 1) + 1):
            for bc in range(max(int((c - reach) // size), 0),
                            min(int((c + reach) // size), lowest[level].shape[1] - 1) + 1):
                stack_level[top], stack_row[top], stack_col[top] = level, br, bc
                top += 1

        near = True
        while top and near:
            top -= 1
            lv, br, bc = stack_level[top], stack_row[top], stack_col[top]
            if lowest[lv][br, bc] >= lowest_allowed:
                continue
            size = block * 4 ** lv
            off_rows = max(br * size - r, 0, r - (br * size + size - 1))
            off_cols = max(bc * size - c, 0, c - (bc * size + size - 1))
            if off_rows * off_rows + off_cols * off_cols > reach * reach:
                continue
            if lv:
                for cr in range(4 * br, min(4 * br + 4, lowest[lv - 1].shape[0])):
                    for cc in range(4 * bc, min(4 * bc + 4, lowest[lv - 1].shape[1])):
                        stack_level[top], stack_row[top], stack_col[top] = lv - 1, cr, cc
                        top += 1
                continue
            b = br * n_block_cols + bc
            for i in range(first[b], first[b] + count[b]):
                d2 = (rows[i] - r) ** 2 + (cols[i] - c) ** 2
                if d2 > reach * reach:
                    continue
                if z[i] + slope[i] * math.sqrt(max(d2 - d * d, 0.0)) < lowest_allowed:
                    near = False
                    break
        out[r, c] = near
        
    return out


def water_mask(workspace: Workspace, dem_raster: Raster, streams: np.ndarray, area_km2: dict | None = None,
               dem: np.ndarray | None = None) -> np.ndarray:
    """
    The bathymetry water mask for ``streams`` (reach ids, 0 off them, draining ``area_km2`` by id) on ``dem_raster``
    (before the burn): the stream cells and the water beside them at about the river's level (see
    near_the_rivers_level), in patches touching a stream cell. The water is the land cover's, and with
    use_dem_derived_channel_mask also the channel the DEM shows (see channel_detection, told by dem_flattens_rivers
    whether the DEM flattens its rivers), where the imagery saw the river elsewhere. The ocean is left out: the burned
    DEM has no data there (see load_ocean_array).

    The water in a lake (see load_lake_array) is the lake's, whatever its level: no stream runs in a lake the network
    is cut at (FLDPLN's), and the river nearest a reservoir's water by its dam is the one below the dam, metres lower.
    A reach the mask has mostly 1 cell wide is made 1 cell wide all along, but in lakes (see
    one_cell_wide_where_mostly). ``dem``, if given, is what ``dem_raster`` holds, already read.
    """
    # Imported here: move_streams imports this module
    from nencarta.tasks.move_streams import load_lake_array, load_ocean_array

    configs = workspace.configs
    if dem is None:
        dem = dem_raster.read_array()
    ocean = load_ocean_array(workspace, dem_raster)
    if ocean is not None:
        streams = np.where(ocean, 0, streams)
    water = np.zeros(dem.shape, bool)
    if workspace.LAND_File.exists():
        water = (Raster(workspace.LAND_File).read_array() == configs.land_watervalue).reshape(dem.shape)
    if configs.use_dem_derived_channel_mask:
        valid = np.isfinite(dem)
        if dem_raster.nodata_value is not None:
            valid &= dem != dem_raster.nodata_value
        if ocean is not None:
            valid &= ~ocean
        water |= dem_channel(dem_raster, dem, valid, streams, area_km2 or {}, flat_water=False,
                             flattens_rivers=configs.dem_flattens_rivers).channel
    if ocean is not None:
        water &= ~ocean
    lakes = load_lake_array(workspace, dem_raster)
    if lakes is None:
        water &= near_the_rivers_level(dem, streams, cells=water)
    else:
        lakes = lakes.reshape(dem.shape)
        water &= lakes | near_the_rivers_level(dem, streams, cells=water & ~lakes)
    mask = remove_cells_not_connected(water | (streams > 0), streams) > 0
    return one_cell_wide_where_mostly(mask, streams, lakes)


def one_cell_wide_where_mostly(mask: np.ndarray, streams: np.ndarray, lakes: np.ndarray | None = None) -> np.ndarray:
    """
    ``mask`` with the reaches of ``streams`` (reach ids, 0 off them) it has mostly 1 cell wide made 1 cell wide all
    along: those more than NARROW_SHARE of whose stream cells have no water among their 8 neighbours but stream cells.
    The patches of water (8-connected, the stream cells aside) that touch only such reaches' stream cells are dropped:
    a pond or a wet field beside a river the DEM and the imagery show no wider than a cell, which ARC would carve as
    the river. A patch that touches a wider reach's stream cells is that river's water, though a narrow reach's line
    runs into it, and is kept; so is one with water in a lake (``lakes``).
    """
    on = streams > 0
    water = mask & ~on
    if not water.any():
        return mask
    cells = np.flatnonzero(on)
    _, of_cell = np.unique(streams.ravel()[cells], return_inverse=True)
    beside_water = np.bincount(of_cell, _beside(water, cells))
    wide = cells[(beside_water >= (1 - NARROW_SHARE) * np.bincount(of_cell))[of_cell]]
    # The patches of water beside a wide reach's stream cells, or with water in a lake
    kept = np.zeros(mask.shape, bool)
    flood_patches(water, wide, 1, kept)
    if lakes is not None:
        flood_patches(water, np.flatnonzero(lakes & water), 0, kept)
    return mask & ~(water & ~kept)


@njit(cache=True)
def _beside(mask: np.ndarray, cells: np.ndarray) -> np.ndarray:
    """Whether each of ``cells`` (flat indices) has a cell of ``mask`` among itself and its eight neighbours."""
    nrows, ncols = mask.shape
    out = np.zeros(cells.size, np.bool_)
    for i in range(cells.size):
        r0, c0 = cells[i] // ncols, cells[i] % ncols
        for r in range(max(r0 - 1, 0), min(r0 + 2, nrows)):
            for c in range(max(c0 - 1, 0), min(c0 + 2, ncols)):
                if mask[r, c]:
                    out[i] = True
                    
    return out


def make_water_mask(workspace: Workspace) -> Path:
    """
    Write the water mask (workspace.bathy_water_mask): 1 on stream cells and the water beside them at the river's
    level (see water_mask), 0 elsewhere.
    """
    configs = workspace.configs
    if workspace.bathy_water_mask.exists() and not configs.overwrite:
        LOG.info(f"{workspace.bathy_water_mask} already exists and we aren't making it again...")
        return workspace.bathy_water_mask

    # make_stream_raster() makes no stream raster when there are no streams in the domain
    if not workspace.DEM_StrmShp.exists() and not configs.raise_errors_if_nothing_in_domain:
        return None

    LOG.info("Creating water mask...")
    workspace.bathy_water_mask.parent.mkdir(parents=True, exist_ok=True)
    
    # ARC carves the bathymetry along the streams it is given, which are the moved ones when the network has been
    # moved to the thalweg, and curve2flood discards whatever bathymetry falls outside this mask. On an overwrite
    # they are moved again, so the last run's are stale; the mask is remade from the new ones once they are.
    if configs.move_stream_network_to_thalweg and workspace.new_stream_raster.exists() and not configs.overwrite:
        stream_ras, stream_lines = Raster(workspace.new_stream_raster), workspace.new_StrmShp_matched
    else:
        stream_ras, stream_lines = Raster(workspace.STRM_File_Clean), workspace.DEM_StrmShp
    streams = stream_ras.read_array()
    area_km2 = {}
    if configs.use_dem_derived_channel_mask and stream_lines.exists():
        table = Vector(stream_lines, use_threads=not configs.parallel).to_geopandas()
        area_km2 = reach_areas_km2(table, configs)

    F = water_mask(workspace, Raster(workspace.assigned_dem), streams, area_km2).astype(np.uint8, copy=False)

    Raster.write_array_using_reference(
        F,
        stream_ras,
        workspace.bathy_water_mask,
        gdal.GDT_Byte,
    )

    return workspace.bathy_water_mask


def flood_streams_to_depth(workspace: Workspace, dem: Path, depth: float, flood_map: Path, config: Path) -> Path:
    """
    Flood the streams ARC runs on to ``depth`` on ``dem`` with Curve2Flood, writing ``flood_map``, and its input file
    ``config``. Each stream cell's water surface is its height plus the depth; each cell takes the average of the stream
    cells' water surfaces within depth_mask_distance of it, weighted by one over the distance squared, and the cells
    below it, in patches touching a stream, are flooded.
    """
    configs = workspace.configs
    stream_raster = workspace.new_stream_raster if configs.move_stream_network_to_thalweg else workspace.STRM_File_Clean
    LOG.info(f"Flooding the streams {depth} m deep...")
    flood_map.parent.mkdir(parents=True, exist_ok=True)
    _write_config(config, {
        "# Curve2Flood_Inputs": "",
        "DEM_File": dem,
        "Stream_File": stream_raster,
        # The FLDPLN mapper can't flood to a set depth
        "mapper": Mapper.CURVE2FLOOD_KERNEL_WEIGHTED,
        "Set_Depth": depth,
        "TopWidthPlausibleLimit": configs.depth_mask_distance,
        "OutFLD": flood_map,
        "Make_Output_GPKG": False,
    })
    Curve2Flood_MainFunction(str(config), quiet=configs.quiet)
    return flood_map


def make_depth_mask(workspace: Workspace) -> Path | None:
    """
    Write ARC's water mask for use_specified_depth_for_bathy_mask (workspace.depth_mask): the streams ARC runs on,
    flooded to the last of specify_depths_for_bathy_mask on the DEM ARC runs on (see flood_streams_to_depth). ARC puts
    the banks where its water ends. Its Curve2Flood input file is workspace.Depth_Mask_Config.
    """
    configs = workspace.configs
    if workspace.depth_mask.exists() and not configs.overwrite:
        LOG.info(f"{workspace.depth_mask} already exists and we aren't making it again...")
        return workspace.depth_mask

    stream_raster = workspace.new_stream_raster if configs.move_stream_network_to_thalweg else workspace.STRM_File_Clean
    # make_stream_raster() makes no stream raster when there are no streams in the domain
    if not stream_raster.exists() and not configs.raise_errors_if_nothing_in_domain:
        return None

    return flood_streams_to_depth(workspace, workspace.model_dem, configs.specify_depths_for_bathy_mask[-1],
                                  workspace.depth_mask, workspace.Depth_Mask_Config)
