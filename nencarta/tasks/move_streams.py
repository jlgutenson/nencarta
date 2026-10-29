"""
Burning the source streams into the DEM, and moving them onto its D8 flow paths (see stream_conflation).
"""
import math
import warnings
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import shapely
from numba import njit
from osgeo import gdal, ogr, osr
from scipy.ndimage import binary_dilation, label
from shapely.geometry import LineString, Point
from shapely.ops import substring

from nencarta.core.raster import Raster
from nencarta.core.vector import Vector
from nencarta.logger import LOG
from nencarta.tasks.make_bathy_water_mask import (
    dem_channel,
    reach_areas_km2,
    water_mask,
)
from nencarta.tasks.make_stream_geometry import _filter_streams_by_stream_order
from nencarta.tasks.stream_conflation import (
    D8_OFFSETS,
    ConflationSettings,
    _line_parts,
    cell_sizes_m,
    conflate_to_flow_directions,
)
from nencarta.tasks.stream_conflation import (
    d8_offset_tables as _d8_offset_tables,
)
from nencarta.tasks.stream_relocation import (
    breach_dammed_rivers,
    relocate_misplaced_reaches,
)
from nencarta.workspace import Workspace


def _in_memory_ogr_layer(gdf: gpd.GeoDataFrame, projection: str,
                         values: pd.Series | None = None) -> tuple[ogr.DataSource, ogr.Layer]:
    """
    Copy ``gdf``'s geometries, in order, into an in-memory OGR layer for GDAL to rasterize, with ``values`` in an
    integer field named "value" if given. Keep the returned DataSource alive while using the layer.
    """
    # 'Memory' is the pre-GDAL-3.11 spelling and now warns on every call; 'MEM' is the
    # current name. Fall back so this still works on older GDAL builds.
    driver: gdal.Driver = ogr.GetDriverByName('MEM') or ogr.GetDriverByName('Memory')
    ogr_ds: ogr.DataSource = driver.CreateDataSource('lakes')
    srs = osr.SpatialReference()
    srs.ImportFromWkt(projection)
    layer: ogr.Layer = ogr_ds.CreateLayer('lakes', srs, ogr.wkbUnknown)
    if values is not None:
        layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger64))
        values = pd.to_numeric(values, errors="coerce").to_numpy()

    layer_defn = layer.GetLayerDefn()
    for i, geometry in enumerate(gdf.geometry.values):
        if geometry is None or geometry.is_empty:
            continue

        feature = ogr.Feature(layer_defn)
        feature.SetGeometry(ogr.CreateGeometryFromWkb(geometry.wkb))
        if values is not None and np.isfinite(values[i]):
            feature.SetField("value", int(values[i]))
        layer.CreateFeature(feature)
        feature = None

    return ogr_ds, layer

def load_lake_array(workspace: Workspace, dem_raster: Raster) -> np.ndarray | None:
    """
    The lakes on the DEM's grid, True in a lake, or None without ``lakes``. Rasterized once to
    workspace.lake_raster, and read back after. The lakes are read with geopandas, since GDAL may not read GeoParquet.
    """
    if not workspace.configs.lakes:
        return None

    if workspace.lake_raster.exists() and (not workspace.configs.overwrite or getattr(workspace, "lake_raster_made", False)):
        # A boolean mask, as below: curve2flood indexes the DEM with it, and a 0/1 integer array would
        # index rows 0 and 1 instead of the lake cells
        lakes = gdal.Open(str(workspace.lake_raster)).ReadAsArray().astype(np.bool_, copy=False)
        return lakes

    lakes_ds: gdal.Dataset = gdal.GetDriverByName('GTiff').Create(str(workspace.lake_raster), dem_raster.shape[1], dem_raster.shape[0], 1, gdal.GDT_Byte, options=[f'COMPRESS={workspace.configs.compression}'])
    lakes_ds.SetGeoTransform(dem_raster.geotransform)
    lakes_ds.SetProjection(dem_raster.projection)

    # Vector.to_geopandas() does the bbox reprojection itself, so the subset comes back
    # already clipped to the DEM's footprint.
    _rasterize_polygons(lakes_ds, load_lake_gdf(workspace, dem_raster))

    lakes_ds.FlushCache()
    lakes = lakes_ds.ReadAsArray().astype(np.bool_, copy=False)
    # Closed now, as it's read back from the file next time
    lakes_ds.Close()
    workspace.lake_raster_made = True
    return lakes

def load_lake_gdf(workspace: Workspace, dem_raster: Raster) -> gpd.GeoDataFrame | None:
    """The lakes overlapping the DEM, or None without ``lakes``."""
    if not workspace.configs.lakes:
        return None

    lakes_gdf = Vector(workspace.configs.lakes, not workspace.configs.parallel).to_geopandas(bbox_epsg_4326=dem_raster.epsg_4326_bbox)

    return lakes_gdf

def load_ocean_array(workspace: Workspace, dem_raster: Raster) -> np.ndarray | None:
    """
    The ocean on the DEM's grid, True in it, or None without ``ocean`` or burn_streams, or with none of it on the
    DEM. The burned DEM has no data there (see smooth_and_burn_dem), so the streams end at the shore. Only the polygons overlapping the DEM are read.
    Made once for each grid, and read-only: the burn and the moving ask for it at every step.
    """
    configs = workspace.configs
    if not configs.ocean or not configs.burn_streams:
        return None

    grid = (configs.ocean, tuple(dem_raster.geotransform), dem_raster.projection, tuple(dem_raster.shape))
    made = getattr(workspace, "ocean_array", None)
    if made is not None and made[0] == grid:
        return made[1]

    ocean = None
    ocean_gdf = Vector(configs.ocean, not configs.parallel).to_geopandas(bbox_epsg_4326=dem_raster.epsg_4326_bbox)
    if not ocean_gdf.empty:
        ocean_ds: gdal.Dataset = gdal.GetDriverByName('MEM').Create('', dem_raster.shape[1], dem_raster.shape[0], 1,
                                                                    gdal.GDT_Byte)
        ocean_ds.SetGeoTransform(dem_raster.geotransform)
        ocean_ds.SetProjection(dem_raster.projection)
        _rasterize_polygons(ocean_ds, ocean_gdf)
        ocean = ocean_ds.ReadAsArray().astype(np.bool_, copy=False)
        if ocean.any():
            ocean.flags.writeable = False
        else:
            ocean = None

    workspace.ocean_array = (grid, ocean)
    return ocean

def _rasterize_polygons(ds: gdal.Dataset, gdf: gpd.GeoDataFrame | None) -> None:
    """Burn 1 into ``ds``'s band in the cells whose centres are in ``gdf``'s polygons."""
    if gdf is None or gdf.empty:
        return
    
    projection = ds.GetProjection()
    ogr_ds, layer = _in_memory_ogr_layer(gdf.to_crs(projection), projection)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gdal.RasterizeLayer(ds, [1], layer, burn_values=[1])

    ogr_ds = None  # noqa: F841

# The burn fills the cells between a line's vertices up to this many cells apart (see _burn_linestring)
MAX_BURN_GAP_CELLS = 3

# Flow directions are D8 pointer codes (see stream_conflation.D8_OFFSETS), Int16 with this nodata
FLOWDIR_NODATA = -32768
FILLED_NODATA = -9999.0
_D8_ROWS = np.array([offset[0] for offset in D8_OFFSETS.values()], np.int64)
_D8_COLS = np.array([offset[1] for offset in D8_OFFSETS.values()], np.int64)
_D8_CODES = np.array(list(D8_OFFSETS), np.int16)


def fill_and_flow_directions(dem: np.ndarray, valid: np.ndarray, geotransform: tuple, projection: str,
                             unfilled: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """
    ``dem`` with its depressions filled (NaN off ``valid``), and its D8 flow directions (see
    stream_conflation.D8_OFFSETS; 0 where nothing around is lower, FLOWDIR_NODATA off the DEM).

    Every depression is filled to its spill level (Priority-Flood, Barnes et al. 2014, see _priority_flood), and the
    flats that leaves -- and the DEM's own, as FABDEM's leveled water -- are given a gradient for the flow directions
    alone (Barnes et al. 2014, "An efficient assignment of drainage direction over flat surfaces", as RichDEM gives it,
    see _resolve_flats): away from the higher ground around them and toward their outlets, so flow paths run down a
    flat's middle rather than straight along one edge. Off
    the DEM, its edges and nodata, is lower than anything on it, so a flat running off it drains there. The gradient
    is imposed in double precision: in single precision its increments round away on a large flat, leaving pits.
    Its increments are a value's last digit, which at 0 m is so small that over a cell's width it is no slope at all,
    so the DEM is raised to 11 m or more first, whole: no difference between two cells changes.

    ``unfilled``, if given, are the only cells (flat indices) that may lie below the DEM's fill: elsewhere on the DEM it
    is filled already, and only these and the cells off it are filled. Lowering a filled DEM's cells can't raise its
    fill anywhere, nor lower it below the DEM itself, so its other cells keep their heights: the burned DEM's routing
    copy is filled but for the lines run down the fill (see routing_dem), and is filled as a whole would fill it.
    """
    valid = np.asarray(valid, bool)
    filled, raise_by = _fill_padded(dem, valid, unfilled)
    flowdir = _padded_flow_directions(filled, valid, geotransform, projection)
    return _unpadded(filled, raise_by, valid), flowdir


def fill_depressions(dem: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """``dem`` with its depressions filled, in double precision (NaN off ``valid``), as fill_and_flow_directions fills it."""
    valid = np.asarray(valid, bool)
    return _unpadded(*_fill_padded(dem, valid), valid)


def _padded_flow_directions(filled: np.ndarray, valid: np.ndarray, geotransform: tuple, projection: str) -> np.ndarray:
    """
    The flow directions of fill_and_flow_directions, from the DEM filled as _fill_padded fills it. Its flats are
    resolved in place for them (see _resolve_flats), and put back.
    """
    raised, before = _raise_flats(filled, _flat_gradient(filled))
    dx, dy = cell_sizes_m(geotransform, projection, valid.shape[0])
    flowdir = _d8_pointers(filled[1:-1, 1:-1], valid, np.asarray(dx, np.float64), float(dy), _D8_ROWS, _D8_COLS,
                           _D8_CODES)
    filled.ravel()[raised] = before
    return flowdir


def _unpadded(filled: np.ndarray, raise_by: float, valid: np.ndarray) -> np.ndarray:
    """The DEM filled as _fill_padded fills it, lowered back and without its ring: NaN off ``valid``."""
    out = filled[1:-1, 1:-1] - raise_by
    out[~valid] = np.nan
    return out


def _fill_padded(dem: np.ndarray, valid: np.ndarray, unfilled: np.ndarray | None = None,
                 put: tuple[np.ndarray, np.ndarray] | None = None) -> tuple[np.ndarray, float]:
    """
    ``dem`` filled (see fill_and_flow_directions) in double precision, inside a ring lower than anything on it, and
    raised by the returned amount. With ``unfilled``, only those cells and the cells off ``valid`` are filled. With
    ``put``, a mask and values, the DEM is np.where(mask, values, dem), without making that copy of it.
    """
    # The lowest valid cell, without a copy of them (SRTM and AW3D30 store whole metres, as integers)
    def lowest(values: np.ndarray, where: np.ndarray):
        return np.min(values, where=where, initial=np.inf if values.dtype.kind == "f" else np.iinfo(values.dtype).max)

    if not valid.any():
        low = 0.0
    elif put is None:
        low = float(lowest(dem, valid)) - 10.0
    else:
        low = float(min(lowest(dem, valid & ~put[0]), lowest(put[1], valid & put[0]))) - 10.0

    raise_by = 1.0 - math.floor(low)
    # The DEM in double precision inside a ring of ``low``. Its own cells off ``valid`` are ``low`` in the DEM's type,
    # as np.where(valid, dem, low) has them.
    padded = np.full((dem.shape[0] + 2, dem.shape[1] + 2), low, np.float64)
    padded[1:-1, 1:-1] = dem
    if put is not None:
        padded[1:-1, 1:-1][put[0]] = put[1][put[0]]

    padded[1:-1, 1:-1][~valid] = np.asarray(low, np.result_type(dem, low))
    padded += raise_by
    closed = np.zeros(padded.shape, np.bool_)
    if unfilled is None:
        closed[[0, -1], :] = closed[:, [0, -1]] = True
        seeds = np.flatnonzero(closed)
    else:
        # Filled already but for these and the cells off the DEM (see fill_and_flow_directions)
        closed[1:-1, 1:-1] = valid
        rows, cols = np.divmod(np.asarray(unfilled, np.int64), dem.shape[1])
        closed[rows + 1, cols + 1] = False
        closed[[0, -1], :] = closed[:, [0, -1]] = True
        seeds = _beside_open(closed)

    _priority_flood(padded, closed.ravel(), seeds)
    return padded, raise_by


@njit(cache=True)
def _beside_open(closed: np.ndarray) -> np.ndarray:
    """The ``closed`` cells (flat indices, in order) with one of their eight neighbours not."""
    nrows, ncols = closed.shape
    beside = np.zeros((nrows, ncols), np.bool_)
    for r in range(nrows):
        for c in range(ncols):
            if closed[r, c]:
                continue

            for rr in range(max(r - 1, 0), min(r + 2, nrows)):
                for cc in range(max(c - 1, 0), min(c + 2, ncols)):
                    if closed[rr, cc]:
                        beside[rr, cc] = True

    return np.flatnonzero(beside)


@njit(cache=True)
def _grown(buffer: np.ndarray, head: int) -> np.ndarray:
    """A ring buffer's contents from ``head`` on, at the start of a buffer twice as big."""
    out = np.empty(buffer.size * 2, buffer.dtype)
    k = buffer.size - head
    out[:k] = buffer[head:]
    out[k:buffer.size] = buffer[:head]
    return out


@njit(cache=True)
def _heap_push(keys: np.ndarray, cells: np.ndarray, size: int, key: float, cell: int) -> int:
    """Push onto a binary min-heap of ``size`` entries with room for one more; returns its new size."""
    i = size
    while i > 0:
        parent = (i - 1) >> 1
        if keys[parent] <= key:
            break

        keys[i] = keys[parent]
        cells[i] = cells[parent]
        i = parent

    keys[i] = key
    cells[i] = cell
    return size + 1


@njit(cache=True)
def _heap_pop(keys: np.ndarray, cells: np.ndarray, size: int) -> int:
    """Take the smallest entry (keys[0], cells[0]) off a binary min-heap of ``size`` entries; returns the new size."""
    size -= 1
    if size > 0:
        key = keys[size]
        cell = cells[size]
        i = 0
        while True:
            left = 2 * i + 1
            if left >= size:
                break
            right = left + 1
            child = right if right < size and keys[right] < keys[left] else left
            if keys[child] >= key:
                break

            keys[i] = keys[child]
            cells[i] = cells[child]
            i = child

        keys[i] = key
        cells[i] = cell

    return size


@njit(cache=True)
def _priority_flood(z: np.ndarray, closed: np.ndarray, seeds: np.ndarray) -> None:
    """
    Fill the depressions of ``z`` (2-D, without nodata) in place, every cell to its spill level: the lowest any
    8-connected way from it to the grid's edge has to climb to. That level is unique, so any Priority-Flood fills alike,
    RichDEM's FillDepressions included, to the last bit: the levels are the DEM's own heights.

    Barnes et al.'s (2014) Priority-Flood floods inward from the edge, lowest cell first. A cell first reached from a
    lower one, up a slope, is at its spill level already, and so is every cell up the slopes from it: these are traced
    without the heap (Zhou et al. 2016). Only the traced cells still beside a cell not reached once the slopes are
    traced -- lower, the edge of a depression -- go on the heap, to fill the depression when the flood reaches them.

    ``closed`` (flat) is True at the cells known to be at their spill level, the grid's edge at least, which are left
    as they are, and ``seeds`` (flat indices) are those of them beside the others: the flood starts from there.
    """
    nrows, ncols = z.shape
    flat = z.ravel()
    # Every cell but the edge's, which are closed from the start, has all eight neighbours on the grid
    offsets = np.array([-ncols - 1, -ncols, -ncols + 1, -1, 1, ncols - 1, ncols, ncols + 1], np.int64)
    heap_keys = np.empty(1024, np.float64)
    heap_cells = np.empty(1024, np.int64)
    heap_size = 0
    # Ring buffers, a power of two long, of the cells whose neighbours are still to be looked at: traced at their own
    # height (trace), and raised to the level being filled (pit). And the traced cells left beside a cell not reached.
    trace = np.empty(1 << 16, np.int64)
    t_head = t_count = 0
    pit = np.empty(1 << 16, np.int64)
    p_head = p_count = 0
    deferred = np.empty(1 << 16, np.int64)
    d_count = 0

    for j in range(seeds.size):
        e = seeds[j]
        row, col = e // ncols, e % ncols
        queued = False
        for r in range(max(row - 1, 0), min(row + 2, nrows)):
            for c in range(max(col - 1, 0), min(col + 2, ncols)):
                m = r * ncols + c
                if closed[m]:
                    continue

                if flat[m] >= flat[e]:
                    closed[m] = True
                    if t_count == trace.size:
                        trace, t_head = _grown(trace, t_head), 0
                    trace[(t_head + t_count) & (trace.size - 1)] = m
                    t_count += 1
                elif not queued:
                    queued = True
                    if heap_size == heap_keys.size:
                        heap_keys, heap_cells = _grown(heap_keys, 0), _grown(heap_cells, 0)
                    heap_size = _heap_push(heap_keys, heap_cells, heap_size, flat[e], e)

    level = -np.inf
    p = -1
    while True:
        # Around the cell taken off the heap (none at first), and the pit it fills: raised to its level, or traced
        while p >= 0 or p_count:
            if p >= 0:
                q, p = p, -1
                row, col = q // ncols, q % ncols
                on_edge = row == 0 or row == nrows - 1 or col == 0 or col == ncols - 1
            else:
                q = pit[p_head]
                p_head = (p_head + 1) & (pit.size - 1)
                p_count -= 1
                on_edge = False

            for k in range(8):
                if on_edge:
                    r = row + (k // 3 if k < 4 else (k + 1) // 3) - 1
                    c = col + (k % 3 if k < 4 else (k + 1) % 3) - 1
                    if r < 0 or r >= nrows or c < 0 or c >= ncols:
                        continue
                    m = r * ncols + c
                else:
                    m = q + offsets[k]

                if closed[m]:
                    continue

                closed[m] = True
                if flat[m] <= level:
                    flat[m] = level
                    if p_count == pit.size:
                        pit, p_head = _grown(pit, p_head), 0
                    pit[(p_head + p_count) & (pit.size - 1)] = m
                    p_count += 1
                else:
                    if t_count == trace.size:
                        trace, t_head = _grown(trace, t_head), 0
                    trace[(t_head + t_count) & (trace.size - 1)] = m
                    t_count += 1

        # Up the slopes from there
        while t_count:
            t = trace[t_head]
            t_head = (t_head + 1) & (trace.size - 1)
            t_count -= 1
            queued = False
            for k in range(8):
                m = t + offsets[k]
                if closed[m]:
                    continue

                if flat[m] >= flat[t]:
                    closed[m] = True
                    if t_count == trace.size:
                        trace, t_head = _grown(trace, t_head), 0
                    trace[(t_head + t_count) & (trace.size - 1)] = m
                    t_count += 1
                elif not queued:
                    queued = True
                    if d_count == deferred.size:
                        deferred = _grown(deferred, 0)
                    deferred[d_count] = t
                    d_count += 1

        for j in range(d_count):
            t = deferred[j]
            for k in range(8):
                if not closed[t + offsets[k]]:
                    if heap_size == heap_keys.size:
                        heap_keys, heap_cells = _grown(heap_keys, 0), _grown(heap_cells, 0)
                    heap_size = _heap_push(heap_keys, heap_cells, heap_size, flat[t], t)
                    break

        d_count = 0
        if heap_size == 0:
            return
        
        level, p = heap_keys[0], heap_cells[0]
        heap_size = _heap_pop(heap_keys, heap_cells, heap_size)


def _raise_flats(z: np.ndarray, steps: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Raise each cell of ``z`` (C-contiguous) by as many units in its last place as its ``steps`` (see _resolve_flats), in
    place. Returns the cells raised (flat indices) and their heights before.
    """
    flat = z.ravel()
    raised = np.flatnonzero(steps.ravel() > 0)
    before = flat[raised]
    up = steps.ravel()[raised]
    ulp = np.spacing(before)
    flat[raised] = before + up * ulp
    # One step at a time past a power of two, where the units grow; never seen on a DEM's values
    for i in np.flatnonzero(np.spacing(flat[raised]) != ulp):
        value = before[i]
        for _ in range(up[i]):
            value = np.nextafter(value, np.inf)

        flat[raised[i]] = value

    return raised, before


@njit(cache=True)
def _flat_gradient(z: np.ndarray) -> np.ndarray:
    """
    The gradient Barnes et al. (2014) give the flats of ``z`` (filled, without nodata), in steps, as RichDEM builds it
    (GetFlatMask): 0 off them.

    A flat is cells with no lower neighbour (the grid's edge drains off it), with an outlet: a cell at its level that
    drains, a low edge. Each flat, the cells at its level joined to a low edge, is labelled. Its cells are numbered by
    how many steps they are from the higher ground around it (its high edges: flat cells beside higher ground),
    reversed so the cells farthest from the higher ground are lowest, and then raised by twice their steps from the
    low edges, which are raised by 2.
    """
    nrows, ncols = z.shape
    d_rows = np.array([0, -1, -1, -1, 0, 1, 1, 1])
    d_cols = np.array([-1, -1, 0, 1, 1, 1, 0, -1])
    no_flow = np.zeros((nrows, ncols), np.bool_)
    n_flat = 0
    for r in range(1, nrows - 1):
        for c in range(1, ncols - 1):
            no_flow[r, c] = True
            for k in range(8):
                if z[r + d_rows[k], c + d_cols[k]] < z[r, c]:
                    no_flow[r, c] = False
                    break

            if no_flow[r, c]:
                n_flat += 1

    # Low edges (drain, beside a flat cell at their level) and high edges (flat cells beside higher ground), found
    # from the flat cells, which are all inside the grid's edge. A low edge beside several is listed once for each: the
    # order and repeats change neither the flats' labels nor the steps through them, found breadth first from all at
    # once.
    low, high = [], []
    for r in range(1, nrows - 1):
        for c in range(1, ncols - 1):
            if not no_flow[r, c]:
                continue

            beside_higher = False
            for k in range(8):
                nr, nc = r + d_rows[k], c + d_cols[k]
                if z[nr, nc] > z[r, c]:
                    beside_higher = True
                elif not no_flow[nr, nc] and z[nr, nc] == z[r, c]:
                    low.append(nr * ncols + nc)

            if beside_higher:
                high.append(r * ncols + c)

    steps = np.zeros((nrows, ncols), np.int32)
    if len(low) == 0:
        return steps

    # Label each flat from its low edges: the cells at its level joined to them. The queue grows as it needs to, to
    # about as many cells as the flats have.
    labels = np.zeros((nrows, ncols), np.int32)
    queue = np.empty(max(n_flat + len(low), 1024), np.int64)
    label = 0
    for start in low:
        r, c = start // ncols, start % ncols
        if labels[r, c]:
            continue

        label += 1
        level = z[r, c]
        labels[r, c] = label
        queue[0] = start
        head, tail = 0, 1
        while head < tail:
            cell = queue[head]
            head += 1
            r, c = cell // ncols, cell % ncols
            for k in range(8):
                nr, nc = r + d_rows[k], c + d_cols[k]
                if 0 <= nr < nrows and 0 <= nc < ncols and not labels[nr, nc] and z[nr, nc] == level:
                    labels[nr, nc] = label
                    if tail == queue.size:
                        queue = _grown(queue, 0)

                    queue[tail] = nr * ncols + nc
                    tail += 1

    # Steps away from the higher ground, through the flat's cells, and the most of them in each flat
    height = np.zeros(label + 1, np.int32)
    tail = 0
    for cell in high:
        r, c = cell // ncols, cell % ncols
        if labels[r, c] and not steps[r, c]:
            steps[r, c] = 1
            if tail == queue.size:
                queue = _grown(queue, 0)

            queue[tail] = cell
            tail += 1

    head = 0
    while head < tail:
        cell = queue[head]
        head += 1
        r, c = cell // ncols, cell % ncols
        height[labels[r, c]] = max(height[labels[r, c]], steps[r, c])
        for k in range(8):
            nr, nc = r + d_rows[k], c + d_cols[k]
            if not (0 <= nr < nrows and 0 <= nc < ncols) or steps[nr, nc]:
                continue

            if labels[nr, nc] == labels[r, c] and no_flow[nr, nc]:
                steps[nr, nc] = steps[r, c] + 1
                if tail == queue.size:
                    queue = _grown(queue, 0)
                queue[tail] = nr * ncols + nc
                tail += 1

    # Steps from the low edges, which are 1, through the flat's cells
    toward = np.zeros((nrows, ncols), np.int32)
    tail = 0
    for cell in low:
        r, c = cell // ncols, cell % ncols
        if not toward[r, c]:
            toward[r, c] = 1
            if tail == queue.size:
                queue = _grown(queue, 0)

            queue[tail] = cell
            tail += 1

    head = 0
    while head < tail:
        cell = queue[head]
        head += 1
        r, c = cell // ncols, cell % ncols
        for k in range(8):
            nr, nc = r + d_rows[k], c + d_cols[k]
            if not (0 <= nr < nrows and 0 <= nc < ncols) or toward[nr, nc]:
                continue

            if labels[nr, nc] == labels[r, c] and no_flow[nr, nc]:
                toward[nr, nc] = toward[r, c] + 1
                if tail == queue.size:
                    queue = _grown(queue, 0)

                queue[tail] = nr * ncols + nc
                tail += 1

    for r in range(nrows):
        for c in range(ncols):
            if not labels[r, c] or not toward[r, c]:
                steps[r, c] = 0
            elif steps[r, c]:
                steps[r, c] = height[labels[r, c]] - steps[r, c] + 2 * toward[r, c]
            else:
                steps[r, c] = 2 * toward[r, c]
                
    return steps


@njit(cache=True)
def _d8_pointers(z: np.ndarray, valid: np.ndarray, dx_by_row: np.ndarray, dy: float, d_rows: np.ndarray,
                 d_cols: np.ndarray, codes: np.ndarray) -> np.ndarray:
    """The code of each valid cell's steepest descent to a valid neighbour (distances in metres), or 0."""
    nrows, ncols = z.shape
    out = np.full((nrows, ncols), FLOWDIR_NODATA, np.int16)
    distance = np.empty(d_rows.size)
    for row in range(nrows):
        for k in range(d_rows.size):
            distance[k] = np.sqrt((dx_by_row[row] * d_cols[k]) ** 2 + (dy * d_rows[k]) ** 2)

        for col in range(ncols):
            if not valid[row, col]:
                continue

            steepest = 0.0
            code = 0
            for k in range(d_rows.size):
                r, c = row + d_rows[k], col + d_cols[k]
                if r < 0 or r >= nrows or c < 0 or c >= ncols or not valid[r, c]:
                    continue

                slope = (z[row, col] - z[r, c]) / distance[k]
                if slope > steepest:
                    steepest = slope
                    code = codes[k]

            out[row, col] = code

    return out


def derive_flow_directions(workspace: Workspace, dem_path: Path, filled_path: Path | None = None,
                           flowdir_path: Path | None = None, dem: np.ndarray | None = None, write: bool = True,
                           unfilled: np.ndarray | None = None,
                           nodata: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """
    Fill the depressions in ``dem_path`` and derive its D8 flow directions (see fill_and_flow_directions), writing
    ``filled_path`` and ``flowdir_path`` (default: workspace.filled_dem and workspace.flowdir) with the configs'
    compression, unless not to ``write`` them yet (see write_flow_directions). ``dem``, if given, is used instead of
    reading ``dem_path``, on its grid (as the burned DEM's routing copy is, see routing_dem), with ``unfilled`` the
    only cells of it that may lie below its fill (see fill_and_flow_directions) and ``nodata`` its nodata value
    (default: ``dem_path``'s). Returns the filled DEM and the flow directions as written, so they needn't be read back.
    """
    raster = Raster(dem_path)
    dem = np.asarray(raster.read_array() if dem is None else dem, np.float64)
    valid = np.isfinite(dem)
    nodata = raster.nodata_value if nodata is None else nodata
    if nodata is not None:
        valid &= dem != nodata

    filled, flowdir = fill_and_flow_directions(dem, valid, raster.geotransform, raster.projection, unfilled)
    del dem

    filled[~valid] = FILLED_NODATA
    if write:
        write_flow_directions(workspace, raster, filled, flowdir, filled_path, flowdir_path)

    return filled, flowdir


def write_flow_directions(workspace: Workspace, reference: Raster, filled: np.ndarray, flowdir: np.ndarray,
                          filled_path: Path | None = None, flowdir_path: Path | None = None) -> None:
    """Write the filled DEM and flow directions derive_flow_directions derives, as it writes them."""
    compression = workspace.configs.compression
    _write_raster(Path(filled_path or workspace.filled_dem), filled, reference, gdal.GDT_Float64, FILLED_NODATA,
                  compression)
    _write_raster(Path(flowdir_path or workspace.flowdir), flowdir, reference, gdal.GDT_Int16, FLOWDIR_NODATA,
                  compression)


def _compression_options(compression: str, data_type: int) -> list[str]:
    """
    The GeoTIFF creation options that compress a raster of ``data_type`` with ``compression``. Floats get the
    floating point predictor and, with ZSTD, level 1. It is written faster and takes 28.6 MiB instead of 32.6.
    """
    options = [f"COMPRESS={compression}"]
    if data_type in (gdal.GDT_Float32, gdal.GDT_Float64) and compression.upper() in ("LZW", "DEFLATE", "ZSTD", "LZMA"):
        options.append("PREDICTOR=3")
        if compression.upper() == "ZSTD":
            options.append("ZSTD_LEVEL=1")
            
    return options


def _write_raster(path: Path, values: np.ndarray, reference: Raster, data_type: int, nodata: float,
                  compression: str) -> None:
    """Write ``values`` as a tiled GeoTIFF on ``reference``'s grid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    driver: gdal.Driver = gdal.GetDriverByName("GTiff")
    if path.exists():
        driver.Delete(str(path))

    ds: gdal.Dataset = driver.Create(str(path), values.shape[1], values.shape[0], 1, data_type,
                                     _compression_options(compression, data_type) + ["TILED=YES", "BIGTIFF=IF_SAFER"])
    ds.SetGeoTransform(reference.geotransform)
    ds.SetProjection(reference.projection)
    band = ds.GetRasterBand(1)
    band.WriteArray(values)
    band.SetNoDataValue(nodata)
    band = None
    # Closed explicitly, as in _write_stream_raster
    ds.Close()

def burn_streams_and_move_streams(workspace: Workspace) -> Path:
    """
    Burn the streams into the DEM (burn_streams), and move them onto its D8 flow paths
    (move_stream_network_to_thalweg). Writes the burned DEM, the filled DEM and flow directions, the moved stream
    network and its raster, and the water mask, plus the stream info table for Curve2Flood-FLDPLNpy.
    """
    if not workspace.DEM_StrmShp.exists() and not workspace.configs.raise_errors_if_nothing_in_domain:
        return None

    configs = workspace.configs
    should_burn_streams = configs.burn_streams and (not workspace.fixed_dem.exists() or configs.overwrite)
    should_move_streams = configs.move_stream_network_to_thalweg and \
        (not workspace.new_StrmShp_matched.exists() or not workspace.new_stream_raster.exists() or configs.overwrite or 
         (configs.mapper.is_curve2flood_fldpln_mapper() and (
            not workspace.stream_info_file.exists() or not workspace.filled_dem.exists() or not workspace.flowdir.exists()
         )))

    assigned_dem = Raster(workspace.assigned_dem)
    flow_directions_ready = False
    unfilled = None  # the cells of dem_for_conflation that may lie below its fill, if only those
    # The filled DEM and flow directions as derived and written this run, which are then not read back
    filled_array = flowdir_array = None
    # The DEM burned this run and its nodata value, when it is written only once restore_unfollowed_burns has decided
    # whether to put cells of it back
    burned = []
    # The assigned DEM, read once for all that reads it here
    unburned = assigned_dem.read_array() if should_burn_streams or should_move_streams else None

    if should_burn_streams:
        workspace.dem_updated_folder.mkdir(parents=True, exist_ok=True)

        lakes = load_lake_array(workspace, assigned_dem)

        source_gdf = Vector(workspace.DEM_StrmShp, not configs.parallel).to_geopandas().to_crs(assigned_dem.projection)
        # Only the reaches the moved network keeps are burned: a line burned and then left out stays carved in the
        # DEM, and floods. Unmoved, the source network is already the one mapped.
        if configs.move_stream_network_to_thalweg:
            source_gdf = _kept_reaches(workspace, source_gdf, assigned_dem)

        # Unmoved, the floods are mapped along the source lines (STRM_File_Clean), so they are burned there too
        if configs.relocate_misplaced_streams and configs.move_stream_network_to_thalweg:
            source_gdf = relocate_misplaced_streams(workspace, source_gdf, assigned_dem, dem=unburned)
        else:
            workspace.relocated_StrmShp.unlink(missing_ok=True)
        # Moved, a reach in pieces is matched along one path through them (see stream_conflation._main_stem), and
        # only its trunk is burned (see _trunks): a branch burned and not matched stays carved in the DEM, and floods
        # The flow directions, and the filled DEM, come from the burned DEM filled, with its lines run down the fill:
        # filled already but where they were (``lowered``)
        lowered = []
        _, dem_for_conflation = smooth_and_burn_dem(
            workspace,
            _trunks(source_gdf, configs.stream_id_field, configs.downstream_id_field, assigned_dem)
            if configs.move_stream_network_to_thalweg else source_gdf,
            lakes,
            configs.stream_id_field, 
            configs.downstream_id_field,
            lowered,
            burned if should_move_streams else None,
            unburned
        )
        unfilled = lowered[0] if lowered else None
        dem_for_conflation_path = workspace.fixed_dem
    elif should_move_streams:
        if configs.burn_streams:
            dem_for_conflation = Raster(workspace.fixed_dem).read_array()
            dem_for_conflation_path = workspace.fixed_dem
        else:
            dem_for_conflation = unburned
            dem_for_conflation_path = workspace.assigned_dem

        if configs.burn_streams and workspace.relocated_StrmShp.exists():
            # The DEM was burned on an earlier run, along these lines. They're left out again: an older run's lines
            # have every reach.
            source_gdf = Vector(workspace.relocated_StrmShp, not configs.parallel).to_geopandas() \
                .to_crs(assigned_dem.projection)
            source_gdf = _kept_reaches(workspace, source_gdf, assigned_dem)
        elif not configs.burn_streams and configs.relocate_misplaced_streams:
            # Unburned, the DEM's flow directions are the ones the streams are moved onto too
            filled_array, flowdir_array = derive_flow_directions(workspace, dem_for_conflation_path,
                                                                 dem=dem_for_conflation)
            flow_directions_ready = True
            source_gdf = Vector(workspace.DEM_StrmShp, not configs.parallel).to_geopandas().to_crs(assigned_dem.projection)
            source_gdf = relocate_misplaced_streams(workspace, _kept_reaches(workspace, source_gdf, assigned_dem),
                                                    assigned_dem, derived=True, dem=unburned)
        else:
            source_gdf = Vector(workspace.DEM_StrmShp, not configs.parallel).to_geopandas().to_crs(assigned_dem.projection)
            source_gdf = _kept_reaches(workspace, source_gdf, assigned_dem)

    if should_move_streams:
        # With the burn, the filled DEM and flow directions are written once restore_unfollowed_burns has decided
        # whether to derive them again, which it does from the filled DEM kept till then
        unwritten = configs.burn_streams and not flow_directions_ready
        if not flow_directions_ready:
            # On the burned DEM's grid, which is the assigned DEM's, while it isn't written yet
            filled_array, flowdir_array = derive_flow_directions(
                workspace, workspace.assigned_dem if burned else dem_for_conflation_path, dem=dem_for_conflation,
                write=not unwritten, unfilled=unfilled, nodata=burned[1] if burned else None)
        streams_gdf, final_streams = move_streams_onto_flow_directions(workspace, source_gdf, dem_for_conflation,
                                                                        assigned_dem, flowdir_array, filled_array)
        if not unwritten:
            filled_array = None
        if streams_gdf.empty:
            if burned:
                write_burned_dem(workspace, *burned)
            if unwritten:
                write_flow_directions(workspace, Raster(dem_for_conflation_path), filled_array, flowdir_array)
            if configs.raise_errors_if_nothing_in_domain:
                raise ValueError("No stream geometries remain after conflation.")
            else:
                workspace.new_StrmShp_matched.unlink(missing_ok=True)
                workspace.DEM_StrmShp = workspace.new_StrmShp_matched
                return None

        kwargs = {'index': False}
        if workspace.new_StrmShp_matched.suffix.lower().endswith('.parquet'):
            kwargs['compression'] = 'brotli'
            kwargs['write_covering_bbox'] = True
            kwargs['geometry_encoding'] = 'geoarrow'

        workspace.new_StrmShp_matched.parent.mkdir(parents=True, exist_ok=True)
        Vector.save_any_geom(streams_gdf, workspace.new_StrmShp_matched, **kwargs)
        if configs.minimize_output_files:
            workspace.DEM_StrmShp.unlink()
            workspace.relocated_StrmShp.unlink(missing_ok=True)
        _write_stream_raster(workspace.new_stream_raster, final_streams, assigned_dem)

        # The water mask is the moved streams and the water at the river's level beside them (see water_mask). ARC
        # carves bathymetry into all of it, so a line burned or drawn where no reach was moved onto (a relocated
        # line the water doesn't take, a reach left out) would be carved, and flood: it is no part of the mask.
        channel_mask = water_mask(workspace, assigned_dem, final_streams,
                                  reach_areas_km2(streams_gdf, configs), unburned).astype(np.uint8)

        workspace.bathy_water_mask.parent.mkdir(parents=True, exist_ok=True)
        make_channel_mask(channel_mask, str(workspace.assigned_dem if burned else dem_for_conflation_path),
                          str(workspace.bathy_water_mask), configs.compression)

        if configs.burn_streams:
            # Nothing is moved onto a line burned where the moved network doesn't run, and it floods
            rederived = restore_unfollowed_burns(workspace, final_streams, channel_mask, dem_for_conflation,
                                                 flowdir_array, filled_array, *(burned or (None, None)), unburned)
            if rederived is not None:
                _, flowdir_array = rederived
            else:
                if burned:
                    write_burned_dem(workspace, *burned)
                if unwritten:
                    write_flow_directions(workspace, Raster(dem_for_conflation_path), filled_array, flowdir_array)
            filled_array = None
            burned = []

    if configs.mapper.is_curve2flood_fldpln_mapper():
        if not should_move_streams:
            # Nothing was regenerated this run, so read back whichever stream raster the
            # rest of the pipeline is pointed at (see params["Stream_File"] in tasks/configs.py).
            if configs.move_stream_network_to_thalweg:
                final_streams = Raster(workspace.new_stream_raster).read_array()
            else:
                final_streams = Raster(workspace.STRM_File_Clean).read_array()

        _create_stream_info_table(
            workspace.stream_info_file,
            streams_array=final_streams,
            flow_direction_file=workspace.flowdir,
            flowdir=flowdir_array,
        )

# How far the filled DEM may change where the streams and their water are before a restored burn is undone (m)
RESTORE_FILL_TOLERANCE_M = 0.01
# How many times the restored burns are filled again, and those that change the streams' drainage undone
RESTORE_PASSES = 4


def restore_unfollowed_burns(workspace: Workspace, final_streams: np.ndarray, channel_mask: np.ndarray,
                             routing: np.ndarray | None = None, flowdir: np.ndarray | None = None,
                             filled_before: np.ndarray | None = None, fixed: np.ndarray | None = None,
                             nodata: float | None = None,
                             unburned: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray] | None:
    """
    Put the unburned DEM (workspace.assigned_dem) back in workspace.fixed_dem wherever the burn changed it and the
    moved network doesn't run: more than a cell from every stream cell of ``final_streams`` and from the water mask
    (``channel_mask``), and outside the lakes. The streams are burned where they are drawn before they are moved, and
    a line the moving doesn't follow stays carved in the DEM, a trench no stream is mapped along, which floods from the
    streams it meets. 

    But some of those cells carry the network's water: a line burned through a dam joins a reservoir to the river
    below it, which starts a few cells below the dam (a lake's rivers stop at its shore), and put back, the dam holds
    the reservoir metres higher in the filled DEM. So the filled DEM is worked out again with the cells put back, and 
    wherever it changes in a stretch of the
    filled DEM that reaches a stream cell or a lake, the burned cells beside that stretch are kept, a connected piece
    at a time, until the filled DEM changes at no stream cell or lake. Not at the water mask's cells: where a line
    put back enters the mask, the cell its routing lowered (see routing_dem) is left a pit, which fills to the water
    around it.

    ``routing``, the DEM the flow directions came from (the burned DEM with its lines run down the fill, see
    routing_dem), gets the unburned DEM where the cells are put back, and the filled DEM and flow directions are
    derived from it again and written (see derive_flow_directions), since the floodplain library is routed across them.
    The moved streams' own cells keep their flow directions (``flowdir``, or workspace.flowdir), which the network was
    moved along and the stream info table walks: a flat the filling leaves can still drain another way. The filled DEM
    they come from is ``filled_before``, or workspace.filled_dem, and the burned DEM ``fixed`` (nodata ``nodata``), or
    workspace.fixed_dem, and ``unburned`` is workspace.assigned_dem's DEM, if read already. Returns the filled DEM and
    flow directions written, or None where nothing was put back, when workspace.fixed_dem is left as it is.
    """
    # Read and closed, not through Raster, which keeps the file open and so couldn't write it again
    def read(path):
        ds: gdal.Dataset = gdal.Open(str(path))
        values, no_value = ds.ReadAsArray(), ds.GetRasterBand(1).GetNoDataValue()
        ds = None
        return values, no_value

    if fixed is None:
        fixed, nodata = read(workspace.fixed_dem)

    assigned_raster = Raster(workspace.assigned_dem)
    unburned = (assigned_raster.read_array() if unburned is None else unburned).astype(fixed.dtype, copy=False)
    valid = np.isfinite(fixed) & np.isfinite(unburned) & (unburned > -1000)
    if nodata is not None:
        valid &= fixed != nodata

    if assigned_raster.nodata_value is not None:
        valid &= unburned != assigned_raster.nodata_value

    streams = np.asarray(final_streams) > 0
    lakes = load_lake_array(workspace, assigned_raster)
    drains = streams if lakes is None else streams | lakes  # what the filled DEM mustn't change at
    eight = np.ones((3, 3), bool)
    restore = valid & (fixed != unburned) & ~binary_dilation(streams | np.asarray(channel_mask, bool), structure=eight)
    if lakes is not None:
        restore &= ~lakes

    if not restore.any():
        return None

    if filled_before is None:
        filled_before, _ = read(workspace.filled_dem)

    if flowdir is None:
        flowdir, _ = read(workspace.flowdir)

    routing = np.asarray(fixed if routing is None else routing, np.float64)
    routing_valid = np.isfinite(routing) & (routing != (-9999 if nodata is None else nodata))
    pieces, _ = label(restore, structure=eight)
    for _ in range(RESTORE_PASSES):
        # Filled as fill_and_flow_directions fills it; its flow directions are only wanted from the last pass
        padded, raise_by = _fill_padded(routing, routing_valid, put=(restore, unburned))
        changed = _fill_changed(padded, raise_by, filled_before, routing_valid, RESTORE_FILL_TOLERANCE_M)
        stretches, _ = label(changed, structure=eight)
        reached = np.unique(stretches[changed & drains])
        if reached.size == 0:
            break

        beside = binary_dilation(np.isin(stretches, reached, kind="table"), structure=eight)
        kept = np.unique(pieces[beside & restore])
        restore &= ~np.isin(pieces, kept, kind="table")
        del padded, changed, stretches, beside
        if not restore.any():
            return None
    else:
        LOG.warning("Some burned cells no moved stream runs along still change how the streams drain when put back; "
                    "they stay burned")
        return None
    
    del changed, stretches, pieces  # noqa: F821

    LOG.info(f"Restoring the unburned DEM at {int(restore.sum())} burned cells no moved stream runs along")
    new_flowdir = _padded_flow_directions(padded, routing_valid, assigned_raster.geotransform, assigned_raster.projection)  # noqa: F821
    filled = _unpadded(padded, raise_by, routing_valid)  # noqa: F821
    del padded  # noqa: F821

    fixed[restore] = unburned[restore]
    _write_raster(Path(workspace.fixed_dem), fixed, assigned_raster, gdal.GDT_Float32,
                  -9999 if nodata is None else nodata, workspace.configs.compression)
    new_flowdir[streams] = flowdir[streams]
    filled[~routing_valid] = FILLED_NODATA
    compression = workspace.configs.compression
    _write_raster(Path(workspace.filled_dem), filled, assigned_raster, gdal.GDT_Float64, FILLED_NODATA, compression)
    _write_raster(Path(workspace.flowdir), new_flowdir, assigned_raster, gdal.GDT_Int16, FLOWDIR_NODATA, compression)
    return filled, new_flowdir


@njit(cache=True)
def _fill_changed(padded: np.ndarray, raise_by: float, before: np.ndarray, valid: np.ndarray,
                  tolerance: float) -> np.ndarray:
    """
    Where the DEM filled as _fill_padded fills it (``padded``, raised by ``raise_by``) is more than ``tolerance`` off
    ``before`` on the ``valid`` cells, as its _unpadded copy would be.
    """
    nrows, ncols = before.shape
    out = np.zeros((nrows, ncols), np.bool_)
    for r in range(nrows):
        for c in range(ncols):
            if valid[r, c]:
                out[r, c] = not (abs((padded[r + 1, c + 1] - raise_by) - before[r, c]) <= tolerance)

    return out


def move_streams_onto_flow_directions(
        workspace: Workspace,
        source_gdf: gpd.GeoDataFrame,
        dem: np.ndarray,
        dem_raster: Raster,
        flowdir: np.ndarray | None = None,
        filled: np.ndarray | None = None) -> tuple[gpd.GeoDataFrame, np.ndarray]:
    """
    Move the source streams onto workspace.flowdir's D8 paths (see stream_conflation), and return them with
    their stream raster. ``source_gdf`` is the reaches to keep (see _kept_reaches). ``flowdir`` and ``filled``, if
    given, are what workspace.flowdir and workspace.filled_dem hold, already read.

    Only flow paths draining at least new_strm_threshold_km2 are matched onto. For FLDPLN, which doesn't map
    lakes, lakes cut the network: no stream runs inside one, and a reach flowing out of one starts below its outlet,
    on workspace.filled_dem (see stream_conflation._below_outlets). A river flowing in across the DEM's edge comes in
    at the river on workspace.filled_dem, not on its bank (see stream_conflation._river_at_edge).
    """
    configs = workspace.configs
    if flowdir is None:
        flowdir = Raster(workspace.flowdir).read_array()

    valid = np.isfinite(dem)
    if dem_raster.nodata_value is not None:
        valid &= dem != dem_raster.nodata_value

    lakes = load_lake_array(workspace, dem_raster)
    cut_lakes = lakes is not None and configs.mapper.is_curve2flood_fldpln_mapper()
    # The DEM the flow directions were derived from, filled, which they run down (see derive_flow_directions): where
    # rivers come in across the edge, and leave lakes
    if filled is None and workspace.filled_dem.exists():
        filled = Raster(workspace.filled_dem).read_array()

    id_col, ds_col = configs.stream_id_field, configs.downstream_id_field
    source_gdf, area_col = _with_area_km2(source_gdf, configs)
    order_col = configs.StrmOrder_Field if configs.StrmOrder_Field in source_gdf.columns else None

    result = conflate_to_flow_directions(
        source_gdf, flowdir, dem_raster.geotransform, dem_raster.projection, id_col, ds_col,
        order_col=order_col, area_col=area_col, lake_mask=lakes if cut_lakes else None, valid_mask=valid,
        settings=ConflationSettings(min_channel_area_km2=configs.new_strm_threshold_km2), dem=filled,
    )
    streams, raster = result.streams, result.raster
    if area_col == "__area_km2":
        streams = streams.drop(columns=[area_col])

    return streams, raster

def _kept_reaches(workspace: Workspace, source_gdf: gpd.GeoDataFrame, dem_raster: Raster) -> gpd.GeoDataFrame:
    """
    ``source_gdf`` without the reaches the moved network leaves out (see _reaches_left_out), and with the ocean, each
    line ending where its river reaches the sea (see _cut_at_the_sea).
    """
    configs = workspace.configs
    left_out = _reaches_left_out(workspace, source_gdf, dem_raster)
    if left_out:
        LOG.info(f"Leaving {len(left_out)} reaches out of the moved stream network.")

    ocean = load_ocean_array(workspace, dem_raster)
    if ocean is not None:
        source_gdf = _cut_at_the_sea(source_gdf, ocean, dem_raster.geotransform, configs.stream_id_field, configs.downstream_id_field)
        
    return _without_reaches(source_gdf, left_out, configs.stream_id_field, configs.downstream_id_field)

def _reaches_left_out(workspace: Workspace, source_gdf: gpd.GeoDataFrame, dem_raster: Raster) -> set:
    """
    The ids of the reaches the moved network leaves out: with drop_multilinestrings those in pieces (see
    _multipart_reaches), with the ocean those below where their river enters the sea (see _reaches_in_the_sea), and
    with the mappers other than FLDPLN those stuck in lakes (see _reaches_stuck_in_lakes) and those outside the
    stream order bounds.

    Decided from the streams as they were before relocation (workspace.DEM_StrmShp, or else ``source_gdf``), before
    anything is relocated, burned or moved, so a rerun decides the same. Left out only after the burn, a reach's
    line stays carved in the DEM, and the floods of the reaches beside it spread into the trench.
    """
    configs = workspace.configs
    id_col, ds_col = configs.stream_id_field, configs.downstream_id_field
    if workspace.DEM_StrmShp.exists():
        source_gdf = Vector(workspace.DEM_StrmShp, not configs.parallel).to_geopandas().to_crs(dem_raster.projection)

    fldpln = configs.mapper.is_curve2flood_fldpln_mapper()

    left_out = set()
    lakes = None if fldpln else load_lake_array(workspace, dem_raster)
    if lakes is not None:
        # FLDPLN cuts the network at the lakes instead (see move_streams_onto_flow_directions)
        dem_ds: gdal.Dataset = gdal.Open(str(workspace.assigned_dem))
        area_col = next((c for c in (configs.area_km2_field, configs.area_m2_field) if c and c in source_gdf.columns),
                        None)
        raster = _get_stream_raster(dem_ds, source_gdf, id_col, configs.StrmOrder_Field, area_col)
        dem_ds = None
        left_out |= _reaches_stuck_in_lakes(source_gdf, raster, lakes, id_col, ds_col)

    ocean = load_ocean_array(workspace, dem_raster)
    if ocean is not None:
        area_col = next((c for c in (configs.area_km2_field, configs.area_m2_field) if c and c in source_gdf.columns),
                        None)
        left_out |= _reaches_in_the_sea(source_gdf, ocean, dem_raster.geotransform, id_col, ds_col,
                                        configs.StrmOrder_Field, area_col)

    if configs.drop_multilinestrings:
        left_out |= _multipart_reaches(source_gdf, id_col)

    if configs.StrmOrder_Field and (configs.StrmOrder_Lower is not None or configs.StrmOrder_Upper is not None) \
            and not fldpln:
        kept = _filter_streams_by_stream_order(source_gdf.copy(), configs.StrmOrder_Field, configs.StrmOrder_Lower,
                                               configs.StrmOrder_Upper)
        left_out |= set(source_gdf[id_col]) - set(kept[id_col])

    return left_out

def _multipart_reaches(source_gdf: gpd.GeoDataFrame, id_col: str) -> set:
    """
    The ids of the reaches whose line is in pieces that can't be joined into one. Decided after a line_merge, not
    from the stored geometry type: a GeoPackage stores every LineString as a MultiLineString. Decide it on the
    streams as they were before relocation, which redraws such a reach as one line, the path of its main stem.
    """
    merged = source_gdf.geometry.line_merge()
    return set(source_gdf.loc[(merged.geom_type == 'MultiLineString').to_numpy(), id_col])

def _trunks(streams: gpd.GeoDataFrame, id_col: str, ds_col: str, dem_raster: Raster) -> gpd.GeoDataFrame:
    """
    ``streams`` with each reach in pieces that can't be joined into one cut to its trunk, the piece with its outlet.

    GEOGLOWS draws its headwater reaches so: the trunk is the reach it routes (its LengthGeodesicMeters), and the
    first-order streams above it are folded in as two branches forking from its top. The moved network matches
    such a reach along its trunk and longer branch (see stream_conflation._main_stem), never the shorter one.

    The outlet is the end nearest the reach it drains into; without one, the end farthest from the reaches draining
    into it; and with neither, the lowest end on ``dem_raster``, an end off it lowest of all. These are the
    conflation's own rules, but for the last, which there is the end the most drains through.
    """
    merged = streams.geometry.line_merge()
    pieces = [_line_parts(line) for line in merged]
    forked = [i for i, parts in enumerate(pieces) if len(parts) > 1]
    if not forked:
        return streams

    lines = dict(zip(streams[id_col], merged))
    upstream = streams.groupby(ds_col)[id_col].agg(list).to_dict()
    dem = None

    def height(xy) -> float:
        nonlocal dem
        if dem is None:
            dem = dem_raster.read_array()

        col, row = gdal.ApplyGeoTransform(gdal.InvGeoTransform(dem_raster.geotransform), xy[0], xy[1])
        row, col = math.floor(row), math.floor(col)
        if not (0 <= row < dem.shape[0] and 0 <= col < dem.shape[1]) or not np.isfinite(dem[row, col]) \
                or dem[row, col] == dem_raster.nodata_value:
            return -np.inf
        
        return float(dem[row, col])

    geometry = streams.geometry.to_numpy().copy()
    ids, ds_ids = streams[id_col].to_numpy(), streams[ds_col].to_numpy()
    for i in forked:
        ends = [(part, Point(xy[:2])) for part in pieces[i] for xy in (part.coords[0], part.coords[-1])]
        above = [lines[r] for r in upstream.get(ids[i], []) if r in lines and r != ids[i]]
        if ds_ids[i] in lines and ds_ids[i] != ids[i]:
            target = lines[ds_ids[i]]
            geometry[i] = min(ends, key=lambda end: end[1].distance(target))[0]
        elif above:
            geometry[i] = max(ends, key=lambda end: min(end[1].distance(line) for line in above))[0]
        else:
            geometry[i] = min(ends, key=lambda end: height(end[1].coords[0]))[0]

    streams = streams.copy()
    streams[streams.geometry.name] = geometry
    return streams


def _with_area_km2(source_gdf: gpd.GeoDataFrame, configs) -> tuple[gpd.GeoDataFrame, str | None]:
    """
    ``source_gdf`` and the name of its drainage area column in km2: area_km2_field, or area_m2_field converted
    into a '__area_km2' column. None when it has neither.
    """
    if configs.area_km2_field and configs.area_km2_field in source_gdf.columns:
        return source_gdf, configs.area_km2_field
    
    if configs.area_m2_field and configs.area_m2_field in source_gdf.columns:
        area = pd.to_numeric(source_gdf[configs.area_m2_field], errors="coerce") / 1e6
        return source_gdf.assign(__area_km2=area), "__area_km2"
    
    return source_gdf, None

def relocate_misplaced_streams(workspace: Workspace, source_gdf: gpd.GeoDataFrame, dem_raster: Raster,
                               derived: bool = False, dem: np.ndarray | None = None) -> gpd.GeoDataFrame:
    """
    Move the reaches the unburned DEM shows are off their river onto its D8 flow paths (see stream_relocation), and
    save the network to workspace.relocated_StrmShp when any line changed. Where the DEM dams a river, the source
    lines are burned through first (see breach_dammed_rivers).

    When ``derived``, workspace.filled_dem and workspace.flowdir are the unburned DEM's, and are used unless it
    dams a river; otherwise the flow directions are derived here. ``dem``, if given, is what ``dem_raster`` holds,
    already read.
    """
    configs = workspace.configs
    workspace.relocated_StrmShp.unlink(missing_ok=True)
    gdf, area_col = _with_area_km2(source_gdf, configs)
    if area_col is None:
        LOG.warning("Misplaced streams weren't relocated: the streams have no drainage area "
                    "(area_km2_field or area_m2_field).")
        return source_gdf

    if dem is None:
        dem = dem_raster.read_array()

    valid = np.isfinite(dem)
    if dem_raster.nodata_value is not None:
        valid &= dem != dem_raster.nodata_value

    # The ocean is off the burned DEM the streams are moved onto (see smooth_and_burn_dem)
    ocean = load_ocean_array(workspace, dem_raster)
    if ocean is not None:
        valid &= ~ocean

    breached, _ = breach_dammed_rivers(gdf, dem, dem_raster.geotransform, dem_raster.projection,
                                       configs.stream_id_field, configs.downstream_id_field, valid_mask=valid)
    ground = dem if breached is None else breached
    if derived and breached is None:
        flowdir_array, filled_array = Raster(workspace.flowdir).read_array(), Raster(workspace.filled_dem).read_array()
    else:
        filled_array, flowdir_array = fill_and_flow_directions(ground, valid, dem_raster.geotransform, dem_raster.projection)
        
    filled_array[~valid] = np.nan
    streams = relocate_misplaced_reaches(gdf, flowdir_array, filled_array, ground, dem_raster.geotransform,
                                         dem_raster.projection, configs.stream_id_field, configs.downstream_id_field,
                                         area_col, valid_mask=valid)
    streams = streams.drop(columns=["__area_km2"], errors="ignore")
    changed = ~streams.geometry.geom_equals(source_gdf.geometry).to_numpy()
    if not changed.any():
        return source_gdf
    
    kwargs = {'index': False}
    if workspace.relocated_StrmShp.suffix.lower() == '.parquet':
        kwargs.update(compression='brotli', write_covering_bbox=True, geometry_encoding='geoarrow')

    workspace.relocated_StrmShp.parent.mkdir(parents=True, exist_ok=True)
    Vector.save_any_geom(streams, workspace.relocated_StrmShp, **kwargs)

    _replace_lines_in_water_mask(workspace, dem_raster, streams[changed], streams.loc[changed, configs.stream_id_field])
    return streams

def _replace_lines_in_water_mask(workspace: Workspace, dem_raster: Raster, redrawn: gpd.GeoDataFrame,
                                 ids: pd.Series) -> None:
    """
    Take the ``redrawn`` reaches' old lines out of the water mask, except where the land cover has water, and put
    their new lines in: the burn smooths the channel along the mask's cells, which should be the lines it burns.
    """
    if not workspace.bathy_water_mask.exists() or not workspace.STRM_File_Clean.exists():
        return
    
    mask = Raster(workspace.bathy_water_mask).read_array().astype(bool)
    strm, ids = Raster(workspace.STRM_File_Clean).read_array(), pd.to_numeric(ids).to_numpy()
    # Looked up at the stream cells alone: the others are 0
    old = np.full(strm.shape, bool(np.isin(0, ids)))
    on = np.flatnonzero(strm)
    old.ravel()[on] = np.isin(strm.ravel()[on], ids)
    del strm

    if workspace.LAND_File.exists():
        old &= Raster(workspace.LAND_File).read_array() != workspace.configs.land_watervalue

    new_ds: gdal.Dataset = gdal.GetDriverByName("MEM").Create("", dem_raster.ncols, dem_raster.nrows, 1, gdal.GDT_Byte)
    new_ds.SetGeoTransform(dem_raster.geotransform)
    new_ds.SetProjection(dem_raster.projection)
    ogr_ds, layer = _in_memory_ogr_layer(redrawn, dem_raster.projection)
    gdal.RasterizeLayer(new_ds, [1], layer, burn_values=[1])
    new = new_ds.ReadAsArray().astype(bool)
    ogr_ds = new_ds = None  # noqa: F841

    mask = (mask & ~old) | new
    make_channel_mask(mask.astype(np.uint8), str(workspace.assigned_dem), str(workspace.bathy_water_mask),
                      workspace.configs.compression)

def _reaches_stuck_in_lakes(streams: gpd.GeoDataFrame, raster: np.ndarray, lakes: np.ndarray, id_col: str,
                            ds_col: str) -> set:
    """
    The reaches that touch a lake, except those that carry a river through it: ones with a reach somewhere above
    them and a reach somewhere below them that touch no lake.
    """
    in_lake = set(np.unique(raster[lakes & (raster > 0)]).tolist())
    if not in_lake:
        return set()
    
    graph = nx.DiGraph()
    graph.add_nodes_from(streams[id_col])
    graph.add_edges_from((r, d) for r, d in zip(streams[id_col], streams[ds_col]) if d in graph and d != r)
    stuck = set()
    for reach in in_lake:
        if not (nx.descendants(graph, reach) - in_lake) or not (nx.ancestors(graph, reach) - in_lake):
            stuck.add(reach)

    return stuck

def _network(streams: gpd.GeoDataFrame, id_col: str, ds_col: str) -> tuple[dict, nx.DiGraph]:
    """Each reach's line (merged), and the graph of which reach flows into which, of the reaches with a line."""
    lines = {reach: line for reach, line in zip(streams[id_col], streams.geometry.line_merge()) if _line_parts(line)}
    graph = nx.DiGraph()
    graph.add_nodes_from(lines)
    graph.add_edges_from((r, d) for r, d in zip(streams[id_col], streams[ds_col]) if r in graph and d in graph and d != r)
    return lines, graph

def _line_start(reach, lines: dict, graph: nx.DiGraph) -> Point | None:
    """
    Where ``reach``'s line starts: its end nearest the reaches flowing into it, or without any, its end farthest from
    the reach it flows into. None without either.
    """
    ends = [Point(part.coords[i][:2]) for part in _line_parts(lines[reach]) for i in (0, -1)]
    above = [lines[r] for r in graph.predecessors(reach)]
    below = [lines[r] for r in graph.successors(reach)]
    if above:
        return min(ends, key=lambda end: min(end.distance(line) for line in above))
    
    if below:
        return max(ends, key=lambda end: below[0].distance(end))
    
    return None

def _reaches_in_the_sea(streams: gpd.GeoDataFrame, ocean: np.ndarray, geotransform: tuple, id_col: str, ds_col: str,
                        order_col: str | None = None, area_col: str | None = None) -> set:
    """
    The reaches below where their river enters the sea: those whose line starts (see _line_start) in the ``ocean`` (a
    mask on the grid of ``geotransform``), and those whose largest upstream reaches (see _largest_upstream_reaches)
    are all in the sea.

    A sea's polygon often reaches up an estuary, leaving its last reaches only the bank beside the channel. Moved onto
    that, each would flood the bank as deep as the river.
    """
    lines, graph = _network(streams, id_col, ds_col)
    inverse = gdal.InvGeoTransform(geotransform)

    def starts_in_the_ocean(reach) -> bool:
        start = _line_start(reach, lines, graph)
        if start is None:
            return False
        
        col, row = gdal.ApplyGeoTransform(inverse, start.x, start.y)
        row, col = math.floor(row), math.floor(col)
        return 0 <= row < ocean.shape[0] and 0 <= col < ocean.shape[1] and bool(ocean[row, col])

    in_sea = {reach for reach in graph if starts_in_the_ocean(reach)}
    largest = _largest_upstream_reaches(graph, streams.set_index(id_col), order_col, area_col)
    reaches = list(in_sea)
    while reaches:
        for reach in graph.successors(reaches.pop()):
            if reach not in in_sea and all(r in in_sea for r in largest[reach]):
                in_sea.add(reach)
                reaches.append(reach)

    return in_sea

def _cut_at_the_sea(streams: gpd.GeoDataFrame, ocean: np.ndarray, geotransform: tuple, id_col: str,
                    ds_col: str) -> gpd.GeoDataFrame:
    """
    ``streams`` with each line ending where, from its start (see _line_start), it first reaches the ``ocean`` (a mask
    on the grid of ``geotransform``): the river ends there, and the rest of its line, along the shore and back onto
    the land at times, is the sea's. Matched onto that land, a reach would flood it as deep as the river. Lines that
    start in the sea (see _reaches_in_the_sea) and lines in pieces are left as they are.
    """
    lines, graph = _network(streams, id_col, ds_col)
    inverse = gdal.InvGeoTransform(geotransform)
    step = abs(geotransform[1]) / 2
    geometry = streams.geometry.to_numpy().copy()
    for i, reach in enumerate(streams[id_col]):
        line = lines.get(reach)
        if line is None or line.geom_type != "LineString":
            continue

        start = _line_start(reach, lines, graph)
        if start is None:
            continue

        forward = start.distance(Point(line.coords[0])) <= start.distance(Point(line.coords[-1]))
        along = np.arange(0.0, line.length, step)
        xy = shapely.get_coordinates(shapely.line_interpolate_point(line, along if forward else line.length - along))
        cols = np.floor(inverse[0] + inverse[1] * xy[:, 0] + inverse[2] * xy[:, 1]).astype(np.int64)
        rows = np.floor(inverse[3] + inverse[4] * xy[:, 0] + inverse[5] * xy[:, 1]).astype(np.int64)
        on = (rows >= 0) & (rows < ocean.shape[0]) & (cols >= 0) & (cols < ocean.shape[1])
        sea = np.zeros(along.size, bool)
        sea[on] = ocean[rows[on], cols[on]]
        if not sea.any() or sea[0]:
            continue

        end = along[np.argmax(sea)]
        geometry[i] = substring(line, 0, end) if forward else substring(line, line.length - end, line.length)

    streams = streams.copy()
    streams[streams.geometry.name] = geometry
    return streams

def _without_reaches(streams: gpd.GeoDataFrame, reaches: set, id_col: str, ds_col: str) -> gpd.GeoDataFrame:
    """``streams`` without ``reaches``; reaches that drained into them become outlets."""
    if not reaches:
        return streams
    
    streams = streams[~streams[id_col].isin(reaches)].copy()
    streams.loc[streams[ds_col].isin(reaches), ds_col] = -1
    return streams

def _write_stream_raster(path: Path, streams: np.ndarray, reference: Raster) -> None:
    """Write the conflated stream ids as an Int32 GeoTIFF on ``reference``'s grid, 0 off the streams."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    driver: gdal.Driver = gdal.GetDriverByName("GTiff")
    if path.exists():
        driver.Delete(str(path))

    stream_ds: gdal.Dataset = driver.Create(str(path), streams.shape[1], streams.shape[0], 1, gdal.GDT_Int32, ['COMPRESS=DEFLATE', 'PREDICTOR=2'])
    stream_ds.SetGeoTransform(reference.geotransform)
    stream_ds.SetProjection(reference.projection)
    stream_ds.GetRasterBand(1).WriteArray(streams.astype(np.int32, copy=False))
    # Closed explicitly: a numba compile earlier in the run can keep this frame, and so the dataset, alive until the
    # next garbage collection, and until then the raster isn't written
    stream_ds.Close()

def smooth_and_burn_dem(
        workspace: Workspace, 
        source_gdf: gpd.GeoDataFrame,
        lakes: np.ndarray | None = None, 
        id_col: str = 'LINKNO',
        ds_col: str = 'DSLINKNO',
        lowered: list | None = None,
        burned: list | None = None,
        unburned: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """
    Burn ``source_gdf``'s streams into the DEM, smooth the burned channels, and write workspace.fixed_dem, or with
    ``burned``, add it and its nodata value to that, to write later (see write_burned_dem). Returns the channel mask and
    the DEM to derive the flow directions from: the burned DEM, filled, with its lines run down the fill (see
    routing_dem; ``lowered``, if given, gets the cells they were run below it at). With use_dem_derived_channel_mask,
    the water the DEM flattens along the streams (see channel_detection) is smoothed as water, and the burn leaves the
    lines on it (see burn_streams_into_dem). ``unburned``, if given, is workspace.assigned_dem's DEM, already read.
    """
    dem_ds: gdal.Dataset = gdal.Open(workspace.assigned_dem)
    dem = dem_ds.ReadAsArray() if unburned is None else unburned.copy()
    nodata_value = dem_ds.GetRasterBand(1).GetNoDataValue()
    if nodata_value is None:
        nodata_value = -9999

    nan_mask = np.isnan(dem)
    dem[nan_mask] = nodata_value
    # The ocean is off the DEM: the lines end at its shore, and the flow directions drain into it
    ocean = load_ocean_array(workspace, Raster(workspace.assigned_dem))
    if ocean is not None:
        dem[ocean] = nodata_value
    del ocean

    configs = workspace.configs
    area_col = next((c for c in (configs.area_km2_field, configs.area_m2_field) if c and c in source_gdf.columns), None)
    streams = _get_stream_raster(dem_ds, source_gdf, id_col, configs.StrmOrder_Field, area_col)

    channel_mask = Raster(workspace.bathy_water_mask).read_array()
    leveled = None
    if workspace.configs.use_dem_derived_channel_mask:
        # The water the DEM flattens along the streams (see channel_detection). Not the low ground by them as well:
        # measured from the lines being burned, that would take in every line the burn has to lower
        leveled = dem_channel(dem_ds, dem, (dem != nodata_value) & np.isfinite(dem), streams,
                              reach_areas_km2(source_gdf, configs), channel=False).flat_water
        channel_mask = channel_mask.astype(bool) | leveled

    original = dem.copy()
    paths, path_reaches = [], []
    kept = np.zeros(dem.shape, bool)
    channel_mask = burn_streams_into_dem(channel_mask, dem, streams, dem_ds, source_gdf, lakes, id_col, ds_col,
                                         nodata_value, order_col=configs.StrmOrder_Field, area_col=area_col,
                                         paths=paths, leveled=leveled, kept=kept, path_reaches=path_reaches)
    del leveled

    dem = smooth_burned_dem(dem, channel_mask, streams, original, nodata_value, paths=paths, fixed=kept,
                            cell_size=cell_sizes_m(dem_ds.GetGeoTransform(), dem_ds.GetProjection(), dem.shape[0]))

    dem[dem < -1000] = nodata_value # Remove any DEM values that are less than -1000 m, since these are likely to be erroneous and will cause problems with the floodplain mapping.
    if burned is None:
        write_burned_dem(workspace, dem, nodata_value)
    else:
        burned.extend((dem, nodata_value))

    if paths:
        # The DEM the flow directions and the filled DEM come from (see routing_dem); ARC keeps the burned DEM
        dem = routing_dem(dem, nodata_value, paths, path_reaches, dem_ds.GetGeoTransform(), dem_ds.GetProjection(),
                          lowered)
    return channel_mask, dem

def write_burned_dem(workspace: Workspace, dem: np.ndarray, nodata_value: float) -> None:
    """Write the burned DEM to workspace.fixed_dem, on workspace.assigned_dem's grid, as smooth_and_burn_dem does."""
    dem_ds: gdal.Dataset = gdal.Open(str(workspace.assigned_dem))
    output_ds: gdal.Dataset = gdal.GetDriverByName('GTiff').Create(workspace.fixed_dem, dem_ds.RasterXSize, dem_ds.RasterYSize, 1, gdal.GDT_Float32, options=_compression_options(workspace.configs.compression, gdal.GDT_Float32))
    output_ds.WriteArray(dem)
    output_ds.SetGeoTransform(dem_ds.GetGeoTransform())
    output_ds.SetProjection(dem_ds.GetProjection())
    output_ds.GetRasterBand(1).SetNoDataValue(nodata_value)
    output_ds = None

# How far below the filled DEM a line may be lowered so that the cell above drains into it rather than onto the ground
# beside (see _run_lines_down_fill). On the water a DEM levels, it takes centimetres; allowed 2 m or more, lines down
# steep valleys were pulled below the slopes beside them.
ROUTING_MAX_DROP_M = 1.0
# How much lower each line cell is than the one above it, so the lines are the lowest way across the flats the fill
# leaves: far more than the steps resolving flats adds (see _resolve_flats), far less than a DEM's precision
ROUTING_STEP_M = 1e-6


def routing_dem(dem: np.ndarray, nodata_value: float, paths: list, path_reaches: list, geotransform: tuple,
                projection: str, lowered: list | None = None) -> np.ndarray:
    """
    The DEM the flow directions and the filled DEM are derived from (see derive_flow_directions): ``dem``, the burned
    DEM, filled (see fill_depressions), in double precision and ``nodata_value`` off the DEM, with each burned line run
    down the fill (see _run_lines_down_fill). ``paths`` and ``path_reaches`` are as burn_streams_into_dem gives them.
    Each line starts below all the reaches flowing into it, not only the largest: a tributary's line left below the
    river at its mouth would take the river's flow down itself. Below each where its line comes nearest the start, as
    burn_linestring takes them: at a junction, its end, but a relocated line can run on past its junction.

    The burn keeps a line on the water the DEM flattens, at the water's level, and a line can come to that water from
    below it, cut deeper upstream (toward lower ground beside it, or below a reach flowing into it). The water then
    dams the line: filled, the line pools behind it, and the pool drains over its lowest rim, off the line at times, a
    reach away. Run down the fill, the line is cut through the water only where the fill rises along it, and only to
    the level the pool spills at, and it is the lowest way across the pool. Lowered instead wherever the burned line
    rose, to its lowest point so far, a line carried that point down the whole network below it until the ground came
    lower.
    """
    valid = (dem != nodata_value) & np.isfinite(dem)
    routing = fill_depressions(dem, valid)
    routing[~valid] = nodata_value
    lines: dict[int, list[np.ndarray]] = {}
    for (_, cells), (reach, _) in zip(paths, path_reaches):
        lines.setdefault(reach, []).append(cells)

    lines = {reach: np.concatenate(cells) for reach, cells in lines.items()}
    starts = [np.unique(np.concatenate([s, np.array([_nearest_cell(lines[f], cells[0], dem.shape[1])
                                                     for f in feeders if f in lines], np.int64)]))
              for (s, cells), (_, feeders) in zip(paths, path_reaches)]
    dx_by_row, dy = cell_sizes_m(geotransform, projection, dem.shape[0])
    cells = np.concatenate([c for _, c in paths])
    fill = routing.ravel()[cells]
    _run_lines_down_fill(routing, np.concatenate(starts), np.cumsum([s.size for s in starts]), cells,
                         np.cumsum([c.size for _, c in paths]), float(nodata_value), ROUTING_STEP_M, ROUTING_MAX_DROP_M,
                         np.asarray(dx_by_row, np.float64), float(dy))
    if lowered is not None:
        lowered.append(np.unique(cells[routing.ravel()[cells] < fill]))

    return routing


def _nearest_cell(cells: np.ndarray, cell: int, ncols: int) -> int:
    """The one of ``cells`` (flat indices on a grid ``ncols`` wide) nearest ``cell``, the last of any tied: of a line
    burned from upstream down, the one farthest down."""
    rows, cols = np.divmod(cells[::-1], ncols)
    row, col = divmod(int(cell), ncols)
    return int(cells[::-1][np.argmin((rows - row) ** 2 + (cols - col) ** 2)])


@njit(cache=True)
def _run_lines_down_fill(values: np.ndarray, starts: np.ndarray, start_ends: np.ndarray, cells: np.ndarray,
                         cell_ends: np.ndarray, nodata_value: float, step: float, max_drop: float,
                         dx_by_row: np.ndarray, dy: float) -> None:
    """
    Run each burned path down ``values`` (the filled DEM, ``nodata_value`` off it), in turn: path i runs down
    cells[cell_ends[i - 1]:cell_ends[i]] (flat indices), starting below the lowest of its start cells,
    starts[start_ends[i - 1]:start_ends[i]], as they are set by then.

    Each cell of a path is ``step`` below the one above it, and never above the fill: a path is cut only where the
    fill rises along it, to the lowest the fill has been above, and across a flat the fill leaves it is the lowest
    way. Each cell is also low enough that the cell above drains into it more steeply than onto the ground beside
    that one, off the paths (see _drain_cap; ``dx_by_row`` is the cells' width on each row and ``dy`` their height, in
    metres), but no more than ``max_drop`` below the fill. A cell on two paths keeps the lower.
    """
    _, ncols = values.shape
    flat = values.ravel()
    on_path = np.zeros(values.shape, np.bool_)
    on_path.ravel()[cells] = True
    done = np.zeros(flat.size, np.bool_)
    fill = flat[cells]
    first_start, first_cell = 0, 0
    for i in range(cell_ends.size):
        level = np.inf
        above = -1
        for k in range(first_start, start_ends[i]):
            s = starts[k]
            if flat[s] != nodata_value and flat[s] < level:
                level = flat[s]
                above = s

        for k in range(first_cell, cell_ends[i]):
            cell = cells[k]
            if flat[cell] == nodata_value:
                continue

            value = min(fill[k], level - step)
            if above >= 0:
                cap = _drain_cap(values, on_path, above // ncols, above % ncols, level, cell // ncols, cell % ncols,
                                 dx_by_row, dy, nodata_value) - step
                value = min(value, max(cap, fill[k] - max_drop))

            if done[cell]:
                value = min(value, flat[cell])

            flat[cell] = value
            done[cell] = True
            level = value
            above = cell

        first_start, first_cell = start_ends[i], cell_ends[i]

@njit(cache=True)
def _label_terraces(values: np.ndarray, use: np.ndarray, n_use: int) -> tuple[np.ndarray, np.ndarray, int]:
    """
    Number the 8-connected groups of ``use`` cells that share one value (0 elsewhere), and list their cells as flat
    indices, group by group.
    """
    nrows, ncols = values.shape
    labels = np.zeros((nrows, ncols), np.int32)
    cells = np.empty(max(n_use, 1), np.int64)
    n = 0
    count = 0
    for r0 in range(nrows):
        for c0 in range(ncols):
            if not use[r0, c0] or labels[r0, c0]:
                continue

            count += 1
            z = values[r0, c0]
            labels[r0, c0] = count
            cells[n] = r0 * ncols + c0
            head = n
            n += 1
            while head < n:
                r, c = cells[head] // ncols, cells[head] % ncols
                head += 1
                for nr in range(max(r - 1, 0), min(r + 2, nrows)):
                    for nc in range(max(c - 1, 0), min(c + 2, ncols)):
                        if use[nr, nc] and labels[nr, nc] == 0 and values[nr, nc] == z:
                            labels[nr, nc] = count
                            cells[n] = nr * ncols + nc
                            n += 1

    return labels, cells[:n], count

@njit(cache=True)
def _distance_to_step(values: np.ndarray, labels: np.ndarray, cells: np.ndarray, index: np.ndarray, max_step: float,
                      up: bool, along: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    For each of ``cells`` (``index`` gives each cell's place among them, -1 elsewhere): how many cells it is, within
    its terrace, from the nearest of its terrace's cells next to a terrace higher (``up``) or lower than it by at most
    ``max_step``, and that terrace's elevation. -1 where there is none. A cell's ``along`` (one per cell, NaN for
    none), the elevation of the cell above it (``up``) or below it along a path, is such a terrace whatever the step,
    and is taken over the cells around it.
    """
    nrows, ncols = values.shape
    n = cells.size
    dist = np.full(n, -1, np.int32)
    other = np.zeros(n, np.float32)
    queue = np.empty(max(n, 1), np.int64)
    tail = 0
    for i in range(n):
        r, c = cells[i] // ncols, cells[i] % ncols
        label, z = labels[r, c], values[r, c]
        best = np.inf
        step = along[i] - z if up else z - along[i]
        if step > 0:  # False for NaN
            best = step
            other[i] = along[i]
        else:
            for nr in range(max(r - 1, 0), min(r + 2, nrows)):
                for nc in range(max(c - 1, 0), min(c + 2, ncols)):
                    if labels[nr, nc] == 0 or labels[nr, nc] == label:
                        continue
                    step = values[nr, nc] - z if up else z - values[nr, nc]
                    if 0 < step <= max_step and step < best:
                        best = step
                        other[i] = values[nr, nc]

        if best < np.inf:
            dist[i] = 0
            queue[tail] = i
            tail += 1

    head = 0
    while head < tail:
        i = queue[head]
        head += 1
        r, c = cells[i] // ncols, cells[i] % ncols
        for nr in range(max(r - 1, 0), min(r + 2, nrows)):
            for nc in range(max(c - 1, 0), min(c + 2, ncols)):
                k = index[nr, nc]
                if k >= 0 and dist[k] < 0 and labels[nr, nc] == labels[r, c]:
                    dist[k] = dist[i] + 1
                    other[k] = other[i]
                    queue[tail] = k
                    tail += 1

    return dist, other

@njit(cache=True)
def _distance_to_bank(cells: np.ndarray, index: np.ndarray) -> np.ndarray:
    """For each of ``cells``: how many cells it is from the nearest cell that isn't one of them (1 next to one)."""
    nrows, ncols = index.shape
    n = cells.size
    dist = np.zeros(n, np.int32)
    queue = np.empty(max(n, 1), np.int64)
    tail = 0
    for i in range(n):
        r, c = cells[i] // ncols, cells[i] % ncols
        for nr in range(max(r - 1, 0), min(r + 2, nrows)):
            for nc in range(max(c - 1, 0), min(c + 2, ncols)):
                if index[nr, nc] < 0 and dist[i] == 0:
                    dist[i] = 1
                    queue[tail] = i
                    tail += 1

    head = 0
    while head < tail:
        i = queue[head]
        head += 1
        r, c = cells[i] // ncols, cells[i] % ncols
        for nr in range(max(r - 1, 0), min(r + 2, nrows)):
            for nc in range(max(c - 1, 0), min(c + 2, ncols)):
                k = index[nr, nc]
                if k >= 0 and dist[k] == 0:
                    dist[k] = dist[i] + 1
                    queue[tail] = k
                    tail += 1

    return dist

def _ramp_terraces(values: np.ndarray, use: np.ndarray, max_step: float, min_cells: int = 1,
                   above: tuple[np.ndarray, np.ndarray] | None = None,
                   below: tuple[np.ndarray, np.ndarray] | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Turn the terraces among ``use`` cells, groups of cells sharing one elevation, into ramps. Where a terrace meets
    one up to ``max_step`` higher or lower, it takes the mean of the two; in its middle, farthest from both, it keeps
    its own elevation; in between, it is interpolated by distance within the terrace. A terrace meeting only higher
    or only lower ones keeps its elevation where it is farthest from them.

    ``above`` and ``below``, if given, are cells (flat indices) and the elevation of the cell above or below each
    along a path (see _path_steps). A terrace meets the next one there whatever the step, rather than among the
    cells around it; with a ``max_step`` of 0, only there.

    Returns the cells of the terraces of at least ``min_cells`` cells (flat indices), their new elevations, and how
    near each is to the middle of its terrace, away from the cells off the terraces: 0 next to one, 1 farthest.
    """
    labels, cells, count = _label_terraces(values, use, int(np.count_nonzero(use)))
    terrace = labels.ravel()[cells]
    if min_cells > 1:
        small = np.bincount(terrace, minlength=count + 1)[terrace] < min_cells
        labels.ravel()[cells[small]] = 0
        cells, terrace = cells[~small], terrace[~small]

    if cells.size == 0:
        return cells, np.empty(0), np.empty(0)
    
    index = np.full(values.shape, -1, np.int32)
    index.ravel()[cells] = np.arange(cells.size, dtype=np.int32)
    along: list[np.ndarray] = []
    for steps, nearest in ((above, np.fmin), (below, np.fmax)):
        z = np.full(cells.size, np.nan)
        if steps is not None:
            k = index.ravel()[steps[0]]
            nearest.at(z, k[k >= 0], steps[1][k >= 0])  # the smallest step, where a cell has several
        along.append(z)

    up, higher = _distance_to_step(values, labels, cells, index, max_step, True, along[0])
    down, lower = _distance_to_step(values, labels, cells, index, max_step, False, along[1])
    bank = _distance_to_bank(cells, index).astype(np.float64)
    del labels, index

    z = values.ravel()[cells].astype(np.float64)
    higher, lower = higher.astype(np.float64), lower.astype(np.float64)
    has_up, has_down = up >= 0, down >= 0
    # Measured from the edge between two terraces, so the cells on either side of it are equally far from it
    up, down = up + 0.5, down + 0.5
    farthest_up = np.zeros(count + 1)
    np.maximum.at(farthest_up, terrace[has_up], up[has_up])
    farthest_down = np.zeros(count + 1)
    np.maximum.at(farthest_down, terrace[has_down], down[has_down])

    new = z.copy()
    k = has_up & has_down & (up <= down)
    new[k] += (higher[k] - z[k]) / 2 * (down[k] - up[k]) / (down[k] + up[k])
    k = has_up & has_down & (up > down)
    new[k] += (lower[k] - z[k]) / 2 * (up[k] - down[k]) / (down[k] + up[k])
    k = has_up & ~has_down
    new[k] += (higher[k] - z[k]) / 2 * (1 - up[k] / farthest_up[terrace[k]])
    k = has_down & ~has_up
    new[k] += (lower[k] - z[k]) / 2 * (1 - down[k] / farthest_down[terrace[k]])

    widest = np.zeros(count + 1)
    np.maximum.at(widest, terrace, bank)
    span = widest[terrace] - 1
    centre = np.divide(bank - 1, span, out=np.zeros_like(bank), where=span > 0)
    return cells, new, centre

def _path_steps(values: np.ndarray, paths: list) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    """
    Where ``values`` (the burned DEM) falls from one cell to the next down ``paths`` (as smooth_burned_dem takes
    them, a path's start cells above its first), for _ramp_terraces: the cells below each drop with the elevation
    above them (``above``), and the cells above each drop with the elevation below them (``below``).

    The cells below a drop are only those on their own path, the last one burned through them: a tributary's line
    ends on the river's, and the river shouldn't ramp up toward it there.
    """
    flat = values.ravel()
    own = np.full(flat.size, -1, np.int32)
    for i, (_, cells) in enumerate(paths):
        own[cells] = i

    tops, under_tops, bottoms, over_bottoms = [], [], [], []
    for i, (starts, cells) in enumerate(paths):
        upper = np.concatenate([starts, cells[:-1]])
        lower = np.concatenate([np.full(starts.size, cells[0]), cells[1:]])
        falls = flat[upper] > flat[lower]
        upper, lower = upper[falls], lower[falls]
        tops.append(upper)
        under_tops.append(flat[lower])
        mine = own[lower] == i
        bottoms.append(lower[mine])
        over_bottoms.append(flat[upper[mine]])

    above = (np.concatenate(bottoms), np.concatenate(over_bottoms).astype(np.float64))
    below = (np.concatenate(tops), np.concatenate(under_tops).astype(np.float64))
    return above, below

def smooth_burned_dem(
        dem: np.ndarray,
        mask: np.ndarray | None = None,
        streams: np.ndarray | None = None,
        original: np.ndarray | None = None,
        nodata_value: float = -9999,
        step: float = 0.5,
        centre_drop: float = 0.1,
        min_water_cells: int = 6,
        paths: list | None = None,
        fixed: np.ndarray | None = None,
        cell_size: tuple[np.ndarray, float] | None = None) -> np.ndarray:
    """
    Smooth the stair steps out of the channels of a burned DEM, and return it.

    Some DEMs, like FABDEM, flatten water into terraces at whole multiples of ``step``, and the burn lowers the stream
    lines in such steps too. Stair steps throw off the stream slopes ARC measures, so each terrace becomes a ramp:
    where it meets the next it takes their mean, and in its middle it keeps its own elevation (see _ramp_terraces).

    The water is the ``mask``'s cells at whole multiples of ``step`` in ``original``, the DEM before the burn, so the
    cells the burn lowered or raised in it are smoothed with the water around them. Its middle, farthest from the
    banks, is lowered by up to ``centre_drop`` for the flow directions. The burned lines (``streams``) are smoothed
    along themselves, and in the water kept as low as its middle, or lower where the burn cut deeper. A bank that a
    smoothed cell beside it would drain onto, at least as steeply as onto the smoothed cells around it, is raised
    0.5 m above where it would (see _bank_levels; ``cell_size`` is the cells' width on each row and their height, in
    metres, see stream_conflation.cell_sizes_m, and square cells without it).

    Given ``paths``, a line's terraces meet where it drops down them, by however much. The burn keeps a line no
    higher than where it has been upstream, so a line can run level for a whole reach and then drop metres where the
    ground does; ramped only toward steps up to ``step``, as the water is, such a terrace stays level.

    The water's ramps can rise along a line the burn kept level, leaving pools along it, so the lines are then made
    never to rise along ``paths``, the lines burn_streams_into_dem burned, in the order it burned them, and to keep
    falling as their own ramps fall. Kept level instead, a line across a pool FABDEM flattened can drain either way
    along itself, and the flow directions take a river back out over the DEM's edge it came in over. The cells the
    burn left (``fixed``, see burn_streams_into_dem) are smoothed as the water around them, and aren't lowered there
    either: a line goes on from where they are.
    """
    dem = dem.astype(np.float32, copy=False)
    original = dem if original is None else original.astype(np.float32, copy=False)
    if mask is None:
        mask = (original % step) == 0

    mask = mask.astype(bool)
    valid = np.isfinite(dem) & (dem != nodata_value)

    water_cells, water, centre = _ramp_terraces(original, mask & valid & (original % step == 0), step, min_water_cells)
    if streams is None:
        line_cells, line = np.empty(0, np.int64), np.empty(0)
    else:
        # A line the burn left on the water (``fixed``) is smoothed as the water around it, not lowered to its middle
        lines = (streams > 0) & valid & (dem % step == 0)
        if fixed is not None:
            lines &= ~np.asarray(fixed, bool)

        if paths:
            # Only the steps down the lines' own paths: lines that touch elsewhere (a tributary beside the river, a
            # bend come back on itself) aren't the next step down either one
            above, below = _path_steps(dem, paths)
            line_cells, line, _ = _ramp_terraces(dem, lines, 0, above=above, below=below)
        else:
            line_cells, line, _ = _ramp_terraces(dem, lines, step)

    out = dem.copy()
    out.ravel()[water_cells] = water - centre_drop * centre
    in_water = np.full(dem.size, -1, np.int32)
    in_water[water_cells] = np.arange(water_cells.size, dtype=np.int32)
    k = in_water[line_cells]
    del in_water

    wet = k >= 0
    ramp = line.copy()
    line[wet] = np.minimum(line[wet], water[k[wet]] - centre_drop)
    out.ravel()[line_cells] = line

    changed = np.zeros(dem.shape, bool)
    changed.ravel()[water_cells] = True
    changed.ravel()[line_cells] = True
    dx_by_row, dy = (np.ones(dem.shape[0]), 1.0) if cell_size is None else cell_size
    beside = _bank_levels(out, changed, np.asarray(dx_by_row, np.float64), float(dy))
    banks = ~mask & ~changed & valid & (out <= beside)
    out[banks] = beside[banks] + 0.5
    if fixed is not None:
        # Where the burn lowered one of them all the same (a line's end at the DEM's edge), it stays as low
        lowered = np.asarray(fixed, bool) & (dem < original)
        out[lowered] = np.minimum(out[lowered], dem[lowered])

    if paths:
        ramps = out.copy()
        ramps.ravel()[line_cells] = ramp
        starts = [start for start, _ in paths]
        cells = [path for _, path in paths]
        _never_rise(out.ravel(), ramps.ravel(), np.concatenate(starts), np.cumsum([s.size for s in starts]),
                    np.concatenate(cells), np.cumsum([c.size for c in cells]),
                    np.zeros(0, np.bool_) if fixed is None else np.ascontiguousarray(fixed, np.bool_).ravel())
        
    return out

@njit(cache=True)
def _bank_levels(values: np.ndarray, changed: np.ndarray, dx_by_row: np.ndarray, dy: float) -> np.ndarray:
    """
    For each cell beside the ``changed`` ones, the highest level at which one of them would drain onto it at least as
    steeply as onto the steepest of the changed cells around it, or as onto level ground if none is lower (-inf
    elsewhere). Distances are in metres: ``dx_by_row`` is the cells' width on each row, ``dy`` their height.

    A cell beside a channel needs to be above this only where it would take the channel's water: ground beside a
    line falling down a slope is lower than the line's cell above it, but the line's next cell is lower still.
    """
    nrows, ncols = values.shape
    levels = np.full((nrows, ncols), -np.inf, np.float32)
    for r0 in range(nrows):
        for c0 in range(ncols):
            if not changed[r0, c0]:
                continue

            z = values[r0, c0]
            slope = 0.0
            for r in range(max(r0 - 1, 0), min(r0 + 2, nrows)):
                for c in range(max(c0 - 1, 0), min(c0 + 2, ncols)):
                    if changed[r, c] and values[r, c] < z:
                        slope = max(slope, (z - values[r, c]) / _distance(r0, c0, r, c, dx_by_row, dy))

            for r in range(max(r0 - 1, 0), min(r0 + 2, nrows)):
                for c in range(max(c0 - 1, 0), min(c0 + 2, ncols)):
                    if not changed[r, c]:
                        levels[r, c] = max(levels[r, c], z - slope * _distance(r0, c0, r, c, dx_by_row, dy))
    return levels

@njit(cache=True, nogil=True)
def _distance(row: int, col: int, row2: int, col2: int, dx_by_row: np.ndarray, dy: float) -> float:
    """The distance in metres between two cells, ``dx_by_row`` wide (on ``row``) and ``dy`` high."""
    return math.sqrt(((col2 - col) * dx_by_row[row]) ** 2 + ((row2 - row) * dy) ** 2)

@njit(cache=True)
def _never_rise(values: np.ndarray, ramps: np.ndarray, starts: np.ndarray, start_ends: np.ndarray,
                cells: np.ndarray, cell_ends: np.ndarray, fixed: np.ndarray) -> None:
    """
    Lower ``values`` (flat) along each path in turn, so that none rises, and each keeps falling where its ramp
    (``ramps``, at least ``values``) falls: path i runs down cells[cell_ends[i - 1]:cell_ends[i]], starting no higher
    than its start cells, starts[start_ends[i - 1]:start_ends[i]]. A path stays as far below its ramp as it has had
    to be anywhere above, its start included. The ``fixed`` cells (flat; none if empty) aren't lowered, and the path
    goes on from them as from its start.
    """
    first_start, first_cell = 0, 0
    for i in range(cell_ends.size):
        level = np.inf
        for k in range(first_start, start_ends[i]):
            level = min(level, values[starts[k]])

        below = ramps[cells[first_cell]] - min(values[cells[first_cell]], level)
        for k in range(first_cell, cell_ends[i]):
            cell = cells[k]
            if fixed.size and fixed[cell]:
                level = values[cell]
                below = ramps[cell] - values[cell]
                continue

            below = max(below, ramps[cell] - values[cell])
            level = min(level, ramps[cell] - below)
            values[cell] = level

        first_start, first_cell = start_ends[i], cell_ends[i]

def _get_stream_raster(dem_ds: gdal.Dataset, streams_gdf: gpd.GeoDataFrame, id_col: str = 'LINKNO',
                       order_col: str | None = None, area_col: str | None = None) -> np.ndarray:
    """
    Rasterize the stream ids onto the DEM's grid.

    A cell where reaches meet holds the last one drawn, and is burned only as deep as that reach. The larger
    reaches (by stream order, then drainage area) are drawn last, so a river keeps the cells it shares with its
    tributaries: a tributary's cell would dam the river, and filling would turn the channel above into a pool.
    """
    keys = [c for c in (order_col, area_col) if c and c in streams_gdf.columns]
    if keys:
        streams_gdf = streams_gdf.sort_values(keys, kind="stable", na_position="first")

    mem_ds: gdal.Dataset = gdal.GetDriverByName('MEM').Create('', dem_ds.RasterXSize, dem_ds.RasterYSize, 1, gdal.GDT_Int32)
    mem_ds.SetGeoTransform(dem_ds.GetGeoTransform())
    mem_ds.SetProjection(dem_ds.GetProjection())
    ogr_ds, stream_layer = _in_memory_ogr_layer(streams_gdf, dem_ds.GetProjection(), values=streams_gdf[id_col])
    gdal.RasterizeLayer(mem_ds, [1], stream_layer, options=["ATTRIBUTE=value"])
    ogr_ds = None  # noqa: F841
    mem_ds.FlushCache()
    streams = mem_ds.ReadAsArray()

    return streams

def burn_streams_into_dem(
        channel_mask: np.ndarray,
        dem: np.ndarray,
        streams: np.ndarray,
        dem_ds: gdal.Dataset,
        streams_gdf: gpd.GeoDataFrame,
        lakes: np.ndarray | None,
        id_col: str = 'LINKNO',
        ds_col: str = 'DSLINKNO',
        nodata_value: float = -9999,
        min_feature_size: int = 5,
        order_col: str | None = None,
        area_col: str | None = None,
        paths: list | None = None,
        leveled: np.ndarray | None = None,
        kept: np.ndarray | None = None,
        path_reaches: list | None = None,
):
    """
    Burn every reach into ``dem`` (in place), upstream reaches first, in 0.5 m steps that never rise downstream,
    each starting no higher than the largest of the reaches flowing into it, by ``order_col`` and then ``area_col``
    (all of them, without either). Returns the channel mask without its specks (``min_feature_size`` cells or
    fewer), lakes and nodata. Appends each burned line to ``paths``, if given (see burn_linestring), and its reach's
    id with the ids of all the reaches flowing into it to ``path_reaches``, if given.

    Each cell is lowered as far as the water in the line's cell above it needs to fall into it rather than onto the
    ground beside (see _drain_cap), and no further: not to the lowest ground beside itself, which ahead of it, down a
    steep line or a cliff, is the ground below its next cell, and never rising, the line stayed that deep downstream.

    A line isn't lowered on ``leveled``, if given, the water the DEM flattens (see channel_detection): the line goes
    on down from such a cell at its level. Except at a line's end on the DEM's edge, which is lowered so the flow leaves there. The cells left so are
    marked in ``kept``, if given.
    """
    G = nx.from_pandas_edgelist(
        streams_gdf[streams_gdf[ds_col] > 0],
        source=id_col,
        target=ds_col,
        create_using=nx.DiGraph
    )
    # A reach with nothing flowing in or out of it is burned too
    G.add_nodes_from(streams_gdf[id_col])
    # Remove any cells that have less than 5 connected neighbors in the mask, as they are likely to be noise or small artifacts
    structure = np.ones((3, 3), dtype=int)
    labels, _ = label(channel_mask, structure=structure)

    counts = np.bincount(labels.ravel())

    keep = counts > min_feature_size
    keep[0] = False
    channel_mask = keep[labels]

    # Mask out lakes
    if lakes is not None:
        channel_mask = channel_mask.astype(bool) & ~lakes

    # Mask out dem's no data value
    if nodata_value is not None:
        channel_mask &= (dem != nodata_value)

    # Burn upstream reaches first. A reach starts no higher than the reaches flowing into it (see
    # _largest_upstream_reaches), which only holds if they were burned before it; in table order a river could be
    # burned before its tributaries had been, leaving a step up where they meet.
    try:
        burn_order = {stream_id: i for i, stream_id in enumerate(nx.topological_sort(G))}
        streams_gdf = streams_gdf.iloc[np.argsort(streams_gdf[id_col].map(burn_order).to_numpy(), kind="stable")]
    except nx.NetworkXUnfeasible:
        LOG.warning("The source stream network has a cycle, so its reaches are burned in table order.")
    streams_gdf = streams_gdf.set_index(id_col)

    dx_by_row, dy = cell_sizes_m(dem_ds.GetGeoTransform(), dem_ds.GetProjection(), dem.shape[0])
    upstream = _largest_upstream_reaches(G, streams_gdf, order_col, area_col)

    # Traverse each segment. Identify which end is up and downstream.
    # Then, take the upstream value. If it is not a multiple of 0.5, lower it to so.
    # Go downstream. Each cell is a multiple of 0.5, no higher than upstream elevation or the current cell elevation.
    for row in streams_gdf.itertuples():
        geom = row.geometry
        stream_id = row.Index
        if geom.geom_type == "MultiLineString":
            lines = geom.geoms
        elif geom.geom_type == "LineString":
            lines = [geom]
        else:
            raise ValueError(f"Unsupported geometry type: {geom.geom_type}")
        
        for line in lines:
            path = burn_linestring(dem, streams, dem_ds, line, stream_id, G, streams_gdf, (dx_by_row, dy),
                                   nodata_value, upstream.get(stream_id, []), leveled, kept)
            if path is not None and paths is not None:
                paths.append(path)
                if path_reaches is not None:
                    path_reaches.append((stream_id, list(G.predecessors(stream_id))))

    return channel_mask

def _largest_upstream_reaches(G: nx.DiGraph, streams_gdf: gpd.GeoDataFrame, order_col: str | None = None,
                              area_col: str | None = None) -> dict:
    """
    Each reach's largest upstream reaches (``streams_gdf`` is indexed by stream id): those flowing into it of the
    highest ``order_col`` and then ``area_col``, as _get_stream_raster ranks them, or all of them without either.

    A reach starts no higher than these. Taking every reach flowing in let a tributary whose mouth lies below the
    river's water, as FABDEM's flattened rivers often have, lower the river below it for kilometres. Such a
    tributary is left in a pool at its mouth instead, as the river's backwater would leave it.
    """
    keys = [c for c in (order_col, area_col) if c and c in streams_gdf.columns]
    sizes = {}
    if keys:
        table = streams_gdf[keys].apply(pd.to_numeric, errors="coerce").fillna(-np.inf)
        table = table.groupby(level=0).max()
        sizes = dict(zip(table.index, table.itertuples(index=False, name=None)))

    upstream = {}
    for reach in G:
        reaches = list(G.predecessors(reach))
        if sizes and len(reaches) > 1:
            largest = max(sizes.get(r, ()) for r in reaches)
            reaches = [r for r in reaches if sizes.get(r, ()) == largest]

        upstream[reach] = reaches

    return upstream

@njit(cache=True, nogil=True)
def nearest_stream_raster_pixel(
    streams_array: np.ndarray,
    start_row: int,
    start_col: int,
    linkno: int
):
    """The cell of reach ``linkno`` nearest the start cell, within one cell of it, or the start cell if none."""
    arr = streams_array
    nrows, ncols = arr.shape

    best_dist = np.inf
    best_r = start_row
    best_c = start_col

    # manually unrolled 3x3 neighborhood
    for r in (start_row - 1, start_row, start_row + 1):
        if r < 0 or r >= nrows:
            continue

        for c in (start_col - 1, start_col, start_col + 1):
            if c < 0 or c >= ncols:
                continue

            if arr[r, c] == linkno:
                dr = c - start_col
                dc = r - start_row
                dist = dr * dr + dc * dc

                if dist < best_dist:
                    best_dist = dist
                    best_r = r
                    best_c = c

    return best_r, best_c

def burn_linestring(
        dem: np.ndarray, 
        streams: np.ndarray, 
        dem_ds: gdal.Dataset, 
        linestring: LineString, 
        linkno: int, 
        G: nx.DiGraph,
        streams_gdf: gpd.GeoDataFrame,
        cell_size: tuple[np.ndarray, float],
        nodata_value: float,
        upstream_ids: list | None = None,
        leveled: np.ndarray | None = None,
        kept: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray] | None:
    """
    Burn one line of reach ``linkno`` into ``dem``, from its upstream end down, starting no higher than the
    reaches ``upstream_ids`` (default: all those flowing into it), and low enough for their water to drain into it
    (see _drain_cap; ``cell_size`` is the cells' width on each row and their height, in metres). The upstream end is
    found from the stream network, or else from the elevations. Its cells on the ``leveled`` water are left, and
    marked in ``kept``.

    Returns the cells of ``upstream_ids`` its start was kept below and the cells it burned, from upstream down,
    as flat indices, or None if it burned nothing.
    """
    if linkno not in G:
        return None  # Skip if the linkno is not in the graph
    
    inverse_transform = gdal.InvGeoTransform(dem_ds.GetGeoTransform())
    coords = np.asarray(linestring.coords)

    # Find the first and last points of the linestring in pixel coordinates
    x1, y1 = coords[0]
    col1, row1 = gdal.ApplyGeoTransform(inverse_transform, x1, y1)
    x2, y2 = coords[-1]
    col2, row2 = gdal.ApplyGeoTransform(inverse_transform, x2, y2)

    # Determine which end is upstream and which is downstream based on topolgy
    if not (0 <= row1 < dem.shape[0] and 0 <= col1 < dem.shape[1] and dem[math.floor(row1), math.floor(col1)] != nodata_value):
        # Traverse the linestring to find the first point that is within the DEM bounds
        for x, y in coords:
            col, row = gdal.ApplyGeoTransform(inverse_transform, x, y)
            if 0 <= row < dem.shape[0] and 0 <= col < dem.shape[1] and dem[math.floor(row), math.floor(col)] != nodata_value:
                col1, row1 = col, row
                break
        else:
            return None  # No valid point found within DEM bounds

    if not (0 <= row2 < dem.shape[0] and 0 <= col2 < dem.shape[1] and dem[math.floor(row2), math.floor(col2)] != nodata_value):
        # Traverse the linestring in reverse to find the last point that is within the DEM bounds
        for x, y in np.flipud(coords):
            col, row = gdal.ApplyGeoTransform(inverse_transform, x, y)
            if 0 <= row < dem.shape[0] and 0 <= col < dem.shape[1] and dem[math.floor(row), math.floor(col)] != nodata_value:
                col2, row2 = col, row
                break
        else:
            return None  # No valid point found within DEM bounds

    row1, col1 = math.floor(row1), math.floor(col1)
    row2, col2 = math.floor(row2), math.floor(col2)
    row1, col1 = nearest_stream_raster_pixel(streams, row1, col1, linkno)
    row2, col2 = nearest_stream_raster_pixel(streams, row2, col2, linkno)

    # Can't rely on elevation to determine upstream/downstream because the DEM may have been modified by previous
    # burns, and a flattened river's two ends are often level. Instead, use the topology of the stream network: the
    # upstream end is the one nearer the reaches flowing in, or else the one farther from the reach this one flows
    # into. Measured by distance rather than by whether an end exactly touches its neighbour, which a line that
    # stops a hair short of its junction never does.
    first_point, last_point = Point(x1, y1), Point(x2, y2)
    flip = None
    neighbours = [n for n in G.predecessors(linkno) if n in streams_gdf.index]
    if neighbours:
        geoms = [_first_geometry(streams_gdf, n) for n in neighbours]
        d_first = min(first_point.distance(g) for g in geoms)
        d_last = min(last_point.distance(g) for g in geoms)
        if d_first != d_last:
            flip = d_last < d_first

    neighbours = [n for n in G.successors(linkno) if n in streams_gdf.index]
    if flip is None and neighbours:
        geom = _first_geometry(streams_gdf, neighbours[0])
        d_first, d_last = first_point.distance(geom), last_point.distance(geom)
        if d_first != d_last:
            flip = d_first < d_last

    if flip is None:
        # Fallback to using elevation to determine upstream/downstream if no upstream or downstream nodes exist.
        flip = bool(dem[row1, col1] < dem[row2, col2])

    if flip:
        coords = np.flipud(coords)
        col1, row1, col2, row2 = col2, row2, col1, row1  # Swap the coordinates as well

    col2, row2 = col1, row1

    # Check if we have any upstream ids.
    if upstream_ids is None:
        upstream_ids = list(G.predecessors(linkno))

    dx_by_row, dy = np.asarray(cell_size[0], np.float64), float(cell_size[1])
    last_elevation = np.inf
    starts = []
    for upstream_id in upstream_ids:
        us_row, us_col = nearest_stream_raster_pixel(streams, row1, col1, upstream_id)
        if 0 <= us_row < dem.shape[0] and 0 <= us_col < dem.shape[1] and dem[us_row, us_col] != nodata_value:
            starts.append(us_row * dem.shape[1] + us_col)
            last_elevation = min(last_elevation, dem[us_row, us_col],
                                 _drain_cap(dem, streams, us_row, us_col, dem[us_row, us_col], row1, col1, dx_by_row,
                                            dy, nodata_value))

    rows, cols = _line_cells(dem, streams, coords, linkno, inverse_transform, row1, col1, nodata_value)
    left = np.zeros(rows.size, np.bool_) if leveled is None else leveled[rows, cols]
    path = _burn_cells(dem, streams, rows, cols, left, last_elevation, nodata_value, dx_by_row, dy)
    if kept is not None:
        kept[rows[left], cols[left]] = True

    return (np.array(starts, np.int64), path) if path.size > 1 else None

def _first_geometry(streams_gdf: gpd.GeoDataFrame, stream_id):
    """A reach's geometry from a gdf indexed by stream id, taking the first row if the id is listed twice."""
    geom = streams_gdf.loc[stream_id, 'geometry']
    return geom.iloc[0] if isinstance(geom, pd.Series) else geom

@njit(cache=True, nogil=True)
def _burn_linestring(
    dem: np.ndarray,
    streams: np.ndarray,
    coords: np.ndarray,
    linkno: int,
    gt: tuple,
    row1: int,
    col1: int,
    last_elevation: float,
    nodata_value: float,
    dx_by_row: np.ndarray,
    dy: float) -> np.ndarray:
    """
    Lower the cells along ``coords`` (``gt`` is the inverse geotransform) to multiples of 0.5 m that never rise
    downstream, starting at (row1, col1) no higher than ``last_elevation`` (see _burn_cells). Returns the cells, as
    flat indices, from upstream down.
    """
    rows, cols = _line_cells(dem, streams, coords, linkno, gt, row1, col1, nodata_value)
    return _burn_cells(dem, streams, rows, cols, np.zeros(rows.size, np.bool_), last_elevation, nodata_value,
                       dx_by_row, dy)


@njit(cache=True, nogil=True)
def _line_cells(
    dem: np.ndarray,
    streams: np.ndarray,
    coords: np.ndarray,
    linkno: int,
    gt: tuple,
    row1: int,
    col1: int,
    nodata_value: float) -> tuple[np.ndarray, np.ndarray]:
    """
    The cells along ``coords`` (``gt`` is the inverse geotransform), from (row1, col1) down: reach ``linkno``'s
    own where the stream raster has one next to a vertex, and the cells between vertices up to MAX_BURN_GAP_CELLS
    apart. Returns their rows and columns.
    """
    xs = coords[:, 0]
    ys = coords[:, 1]
    cols_ = np.floor(gt[0] + gt[1] * xs + gt[2] * ys).astype(np.int64)
    rows_ = np.floor(gt[3] + gt[4] * xs + gt[5] * ys).astype(np.int64)

    nrows, ncols = dem.shape

    rows = [row1]
    cols = [col1]
    last_row = row1
    last_col = col1

    for row, col in zip(rows_[1:], cols_[1:]):
        row, col = nearest_stream_raster_pixel(streams, row, col, linkno)

        if not (0 <= row < nrows and 0 <= col < ncols and dem[row, col] != nodata_value):
            continue

        if row != last_row or col != last_col:
            # Vertices more than a cell apart (a line drawn a cell's length in metres apart, where cells are
            # narrower than that one way) would skip the cells between them, and an unburned cell dams the channel.
            # Burn the cells between them too, the reach's own where the stream raster has one. A longer gap is
            # where the line left the DEM and came back, and is left alone.
            steps = max(abs(row - last_row), abs(col - last_col))
            if 1 < steps <= MAX_BURN_GAP_CELLS:
                for k in range(1, steps):
                    fr = last_row + (row - last_row) * k / steps
                    fc = last_col + (col - last_col) * k / steps
                    r = math.floor(fr + 0.5)
                    c = math.floor(fc + 0.5)
                    for rr in (math.floor(fr), math.ceil(fr)):
                        for cc in (math.floor(fc), math.ceil(fc)):
                            if 0 <= rr < nrows and 0 <= cc < ncols and streams[rr, cc] == linkno:
                                r, c = rr, cc
                    if 0 <= r < nrows and 0 <= c < ncols and dem[r, c] != nodata_value:
                        rows.append(r)
                        cols.append(c)

            rows.append(row)
            cols.append(col)
            last_row = row
            last_col = col

    return np.array(rows, np.int64), np.array(cols, np.int64)


@njit(cache=True, nogil=True)
def _burn_cells(
    dem: np.ndarray,
    streams: np.ndarray,
    rows: np.ndarray,
    cols: np.ndarray,
    keep: np.ndarray,
    last_elevation: float,
    nodata_value: float,
    dx_by_row: np.ndarray,
    dy: float) -> np.ndarray:
    """
    Lower a line's cells (``rows``, ``cols``, from upstream down) to multiples of 0.5 m that never rise downstream,
    starting no higher than ``last_elevation``, but for the ones to ``keep``: the line goes on down from those at
    their own level. Each cell is also as low as the water in the cell above needs to fall into it rather than onto
    the ground beside (see _drain_cap; ``dx_by_row`` is the cells' width on each row and ``dy`` their height, in
    metres), and ground beside that would take the water as steeply, as level ground beside a level line does, is
    raised 0.5 m above where it would. Returns the cells, as flat indices, from upstream down.
    """
    nrows, ncols = dem.shape
    has_written = False

    # The trailing edge-of-DEM check below reads state from the last iteration of the loop.
    # A single-pixel reach never enters that loop, so these have to exist beforehand -- and
    # numba needs them bound on every path to compile the function at all. `has_written`
    # gates their use, so the seed values are never the ones acted on.
    row2 = rows[0]
    col2 = cols[0]

    # Every cell of the line is burned, even where it leaves the channel mask: an unburned gap dams the channel,
    # and filling turns the channel above it into a flat pool
    for k in range(rows.size - 1):
        row1, col1, row2, col2 = rows[k], cols[k], rows[k + 1], cols[k + 1]
        has_written = True

        upstream_elev = dem[row1, col1]
        downstream_elev = dem[row2, col2]

        if not keep[k]:
            upstream_elev = min(upstream_elev, last_elevation)

            if upstream_elev % 0.5 != 0:
                upstream_elev = math.floor(upstream_elev * 2) / 2

            dem[row1, col1] = upstream_elev

        if not keep[k + 1]:
            # Lowered to the ground beside the cell above only as far as its water would fall onto that ground
            # instead. Lowered to the lowest ground beside itself, a cell at the lip of a cliff was cut to the cliff's
            # foot, and the line stayed that deep downstream until the ground came down to it.
            cap = _drain_cap(dem, streams, row1, col1, upstream_elev, row2, col2, dx_by_row, dy, nodata_value)
            downstream_elev = min(downstream_elev, cap)
            if downstream_elev > upstream_elev:
                downstream_elev = upstream_elev
            elif downstream_elev % 0.5 != 0:
                downstream_elev = math.floor(downstream_elev * 2) / 2

            dem[row2, col2] = downstream_elev

        last_elevation = downstream_elev
        if keep[k]:
            continue  # left as the DEM has it, and the ground around it too

        # Ground beside this cell that would still take its water as steeply as the next cell does, as level ground
        # beside a level line does, is raised 0.5 m above where it would
        drop = upstream_elev - downstream_elev
        if drop < 0:
            continue

        step = _distance(row1, col1, row2, col2, dx_by_row, dy)
        for r in range(max(row1 - 1, 0), min(row1 + 2, nrows)):
            for c in range(max(col1 - 1, 0), min(col1 + 2, ncols)):
                if streams[r, c] > 0 or dem[r, c] == nodata_value or (r == row1 and c == col1) or \
                        (r == row2 and c == col2):
                    continue

                distance = _distance(row1, col1, r, c, dx_by_row, dy)
                if (upstream_elev - dem[r, c]) * step >= drop * distance:
                    dem[r, c] = upstream_elev - drop * distance / step + 0.5

    # A reach that leaves the DEM, over its edge or into nodata, ends 0.5 m lower, so the filled DEM routes
    # out there. Only there: each reach starts no higher than the reaches flowing into it, so lowering every reach's
    # end lowered the whole network below it 0.5 m at each junction, metres below a flat river's water downstream
    if has_written and dem[row2, col2] != nodata_value and (
            row2 == 0 or row2 == nrows - 1 or col2 == 0 or col2 == ncols - 1
            or (dem[row2 - 1:row2 + 2, col2 - 1:col2 + 2] == nodata_value).any()):
        dem[row2, col2] -= 0.5

    return rows * ncols + cols

@njit(cache=True, nogil=True)
def _drain_cap(dem: np.ndarray, streams: np.ndarray, row: int, col: int, level: float, row2: int, col2: int,
               dx_by_row: np.ndarray, dy: float, nodata_value: float) -> float:
    """
    How high the cell (row2, col2) can be for the water in (row, col), at ``level``, to fall into it at least as
    steeply as onto the ground beside (row, col), off the stream raster: inf if no ground beside is lower.

    The D8 flow directions take the steepest way down, so the line holds the water to here. Where the ground beside
    falls away faster than the line, over a canyon's rim, the line is cut as deep as the canyon all the same.
    """
    nrows, ncols = dem.shape
    step = _distance(row, col, row2, col2, dx_by_row, dy)
    cap = np.inf
    for r in range(max(row - 1, 0), min(row + 2, nrows)):
        for c in range(max(col - 1, 0), min(col + 2, ncols)):
            if streams[r, c] > 0 or dem[r, c] == nodata_value or dem[r, c] >= level:
                continue

            cap = min(cap, level - (level - dem[r, c]) * step / _distance(row, col, r, c, dx_by_row, dy))

    return cap

# How far the D8 path may run outside a reach's cells before the reach is finished. A moved reach never leaves
# its D8 path, but a stream raster drawn from vector lines strays off it by a cell or so.
_MAX_OFF_REACH_STEPS = 2

@njit(cache=True)
def _trace_reach(cells: np.ndarray, flowdir: np.ndarray, nrows: int, ncols: int,
                 row_offsets: np.ndarray, col_offsets: np.ndarray,
                 max_off_reach_steps: int) -> tuple[int, int, int]:
    """
    Follow the D8 flow directions through one reach's cells (sorted flat indices).

    Returns ``(start_pixel, end_pixel, length)``: where the longest D8 path through the cells starts, its last
    cell in the reach, and how many cells it visits. Walking ``length`` cells down from ``start_pixel`` ends on
    ``end_pixel``.
    """
    n = cells.size

    # link[i] is the next cell of this reach that cells[i] drains into (-1 if the
    # path leaves the reach for good), and gap[i] is how many D8 steps that takes.
    # It is usually one step; more when the rasterized line strays off the D8 path.
    link = np.full(n, -1, np.int64)
    gap = np.zeros(n, np.int64)
    for i in range(n):
        pixel = cells[i]
        for step in range(1, max_off_reach_steps + 1):
            code = flowdir[pixel]
            if code < 0 or code > 255:
                break

            d_row = row_offsets[code]
            d_col = col_offsets[code]
            if d_row == 0 and d_col == 0:  # pit, outlet, or nodata
                break

            row = pixel // ncols + d_row
            col = pixel % ncols + d_col
            if row < 0 or row >= nrows or col < 0 or col >= ncols:
                break

            pixel = row * ncols + col
            j = np.searchsorted(cells, pixel)
            if j < n and cells[j] == pixel:
                link[i] = j
                gap[i] = step
                break

    # Every cell has at most one successor, so the reach forms a forest. Score each
    # cell by the path leading out of it and keep whichever root covers the most of
    # the reach: that is its head, and the far end of its path is its outlet.
    covered = np.zeros(n, np.int64)   # cells of this reach on the path out of i
    path_len = np.zeros(n, np.int64)  # D8 steps from i to the end of that path
    terminus = np.arange(n)
    state = np.zeros(n, np.uint8)     # 0 unvisited, 1 on the stack, 2 resolved
    stack = np.empty(n, np.int64)
    for root in range(n):
        if state[root] != 0:
            continue

        top = 0
        stack[0] = root
        state[root] = 1
        while top >= 0:
            i = stack[top]
            j = link[i]
            if j >= 0 and state[j] == 0:
                top += 1
                stack[top] = j
                state[j] = 1
                continue

            if j < 0 or state[j] == 1:  # end of the path, or a cycle across a flat
                link[i] = -1
                covered[i] = 1
                path_len[i] = 0
                terminus[i] = i
            else:
                covered[i] = 1 + covered[j]
                path_len[i] = gap[i] + path_len[j]
                terminus[i] = terminus[j]

            state[i] = 2
            top -= 1

    head = 0
    for i in range(1, n):
        if covered[i] > covered[head] or (covered[i] == covered[head] and path_len[i] > path_len[head]):
            head = i

    return cells[head], cells[terminus[head]], path_len[head] + 1

def _create_stream_info_table(
        stream_info_file: Path,
        streams_array: np.ndarray,
        flow_direction_file: str | Path,
        flowdir: np.ndarray | None = None) -> None:
    """
    Write the stream info table Curve2Flood's FLDPLN mapper reads (``flowdir``, if given, is what
    ``flow_direction_file`` holds, already read).

    FLDPLN walks ``length`` cells down the D8 flow directions from each reach's ``start_pixel`` to find its
    stream cells, so each row must be a real D8 path that ends on ``end_pixel``. The table is therefore traced
    from the flow direction raster, not from the stream lines.
    """
    flow_direction_file = Path(flow_direction_file)
    if not flow_direction_file.exists():
        raise FileNotFoundError(
            f"Flow direction raster {flow_direction_file} does not exist. The FLDPLN stream info table is "
            "traced from it, so the hydrography has to be derived first (move_stream_network_to_thalweg)."
        )

    if flowdir is None:
        flowdir = gdal.Open(str(flow_direction_file)).ReadAsArray()
        
    if flowdir.shape != streams_array.shape:
        raise ValueError(
            f"Flow direction raster {flow_direction_file} is {flowdir.shape} but the stream raster is "
            f"{streams_array.shape}. Both have to be on the DEM grid for the pixel indices to line up."
        )

    nrows, ncols = streams_array.shape
    linknos_flat = np.ascontiguousarray(streams_array).ravel()
    flowdir_flat = np.ascontiguousarray(flowdir).ravel()
    row_offsets, col_offsets = _d8_offset_tables()

    # Group the stream pixels into one sorted block per link number.
    stream_pixels = np.flatnonzero(linknos_flat > 0)
    stream_pixels = stream_pixels[np.argsort(linknos_flat[stream_pixels], kind='stable')]
    linknos, block_starts = np.unique(linknos_flat[stream_pixels], return_index=True)
    block_ends = np.append(block_starts[1:], stream_pixels.size)

    output_table = []
    for linkno, block_start, block_end in zip(linknos, block_starts, block_ends):
        cells = np.sort(stream_pixels[block_start:block_end])
        start_pixel, end_pixel, length = _trace_reach(
            cells, flowdir_flat, nrows, ncols, row_offsets, col_offsets, _MAX_OFF_REACH_STEPS
        )
        output_table.append((int(start_pixel), int(end_pixel), int(length), int(linkno)))

    stream_info = pd.DataFrame(output_table,
                 columns=['start_pixel', 'end_pixel', 'length', 'stream_id'])
    stream_info_file.parent.mkdir(parents=True, exist_ok=True)
    if stream_info_file.suffix.lower() in {".parquet", ".pq"}:
        stream_info.to_parquet(stream_info_file, index=False)
    else:
        stream_info.to_csv(stream_info_file, index=False)


def make_channel_mask(channel_mask: np.ndarray, dem: str, water_mask: str, compression: str):
    """Write ``channel_mask`` to ``water_mask`` as a Byte raster on the DEM's grid."""
    dem_ds: gdal.Dataset = gdal.Open(dem)
    out_ds: gdal.Dataset = gdal.GetDriverByName('GTiff').Create(water_mask, dem_ds.RasterXSize, dem_ds.RasterYSize, 1, gdal.GDT_Byte, options=[f'COMPRESS={compression}'])
    out_ds.WriteArray(channel_mask)
    out_ds.SetGeoTransform(dem_ds.GetGeoTransform())
    out_ds.SetProjection(dem_ds.GetProjection())
    out_ds = None
