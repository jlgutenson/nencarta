import os
import math
import warnings
from enum import Enum
from pathlib import Path
from dataclasses import dataclass
from itertools import combinations
from collections import defaultdict
from functools import partial

import tqdm
import numpy as np
import pandas as pd
import networkx as nx
from numba import njit
import geopandas as gpd
from osgeo import gdal, ogr, osr
from shapely.ops import substring
from whitebox import WhiteboxTools
from shapely import line_merge, prepare
from scipy.ndimage import binary_dilation, distance_transform_edt, label, minimum_filter
from shapely.geometry import box, Point, LineString, Polygon, MultiLineString, GeometryCollection

from nencarta.logger import LOG
from nencarta.core.raster import Raster
from nencarta.core.vector import Vector
from nencarta.tasks.make_stream_geometry import _filter_streams_by_stream_order
from nencarta.workspace import Workspace
from curve2flood import remove_cells_not_connected

def _in_memory_ogr_layer(gdf: gpd.GeoDataFrame, projection: str) -> tuple[ogr.DataSource, ogr.Layer]:
    """
    Copy ``gdf``'s geometries into an in-memory OGR layer so GDAL can rasterize them.

    The caller must keep the returned DataSource alive: OGR owns the layer, and letting the
    DataSource fall out of scope invalidates the layer while it is still in use.
    """
    # 'Memory' is the pre-GDAL-3.11 spelling and now warns on every call; 'MEM' is the
    # current name. Fall back so this still works on older GDAL builds.
    driver = ogr.GetDriverByName('MEM') or ogr.GetDriverByName('Memory')
    ogr_ds: ogr.DataSource = driver.CreateDataSource('lakes')
    srs = osr.SpatialReference()
    srs.ImportFromWkt(projection)
    layer: ogr.Layer = ogr_ds.CreateLayer('lakes', srs, ogr.wkbUnknown)
    layer_defn = layer.GetLayerDefn()
    for geometry in gdf.geometry.values:
        if geometry is None or geometry.is_empty:
            continue
        feature = ogr.Feature(layer_defn)
        feature.SetGeometry(ogr.CreateGeometryFromWkb(geometry.wkb))
        layer.CreateFeature(feature)
        feature = None
    return ogr_ds, layer

def load_lake_array(workspace: Workspace, dem_raster: Raster) -> np.ndarray | None:
    """
    We load the lakes in the DEM's domain. We do not want to include lakes/reservoirs.

    The lakes are read through :class:`Vector` rather than ``ogr.Open`` so that every vector
    format nencarta accepts elsewhere works here too. ``ogr.Open`` cannot read a GeoParquet
    lakes file unless GDAL was built with the Parquet/Arrow driver; without it GDAL falls
    through to the ADBC driver and dies on a missing ``duckdb.dll``, which took out the whole
    burn/move step for the default (GeoParquet) lakes layer.
    """
    if not workspace.configs.lakes:
        return None

    if workspace.lake_raster.exists() and not workspace.configs.overwrite:
        lakes = gdal.Open(str(workspace.lake_raster)).ReadAsArray().astype(np.uint8, copy=False)
        return lakes

    lakes_ds: gdal.Dataset = gdal.GetDriverByName('GTiff').Create(str(workspace.lake_raster), dem_raster.shape[1], dem_raster.shape[0], 1, gdal.GDT_Byte, options=[f'COMPRESS={workspace.configs.compression}'])
    lakes_ds.SetGeoTransform(dem_raster.geotransform)
    lakes_ds.SetProjection(dem_raster.projection)

    # Vector.to_geopandas() does the bbox reprojection itself, so the subset comes back
    # already clipped to the DEM's footprint.
    lakes_gdf = load_lake_gdf(workspace, dem_raster)
    if lakes_gdf is not None and not lakes_gdf.empty:
        lakes_gdf = lakes_gdf.to_crs(dem_raster.projection)
        ogr_ds, lakes_layer = _in_memory_ogr_layer(lakes_gdf, dem_raster.projection)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            gdal.RasterizeLayer(lakes_ds, [1], lakes_layer, burn_values=[1])

        ogr_ds = None

    lakes_ds.FlushCache()
    lakes = lakes_ds.ReadAsArray().astype(np.bool_, copy=False)
    return lakes

def load_lake_gdf(workspace: Workspace, dem_raster: Raster) -> gpd.GeoDataFrame | None:
    if not workspace.configs.lakes:
        return None

    lakes_gdf = Vector(workspace.configs.lakes, not workspace.configs.parallel).to_geopandas(bbox_epsg_4326=dem_raster.epsg_4326_bbox)

    return lakes_gdf

def whitebox_callback(message: str) -> None:
    """
    Callback function for WhiteboxTools to log messa`ges.
    We only want to log errors and warnings, so we filter out other messages.
    """
    lowered = message.lower()
    if "warning" in lowered and not lowered.endswith("It appears that the input data is in"):
        LOG.warning(message)
    if "error" in lowered or "panic" in lowered:
        LOG.error(message) 

class _WhiteboxOutput:
    """Collects WhiteboxTools messages so a failure can report what the tool actually said."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(message)
        whitebox_callback(message)


def _run_whitebox(tool, expected: Path, description: str, *args, **kwargs) -> int:
    """
    Run one WhiteboxTools tool and fail loudly if it did not produce ``expected``.

    The WhiteboxTools Python wrapper reads the tool's stdout until EOF and then returns 0
    unconditionally -- it never waits on the child or looks at its exit status. With verbose
    mode off the tool prints nothing at all, so a crash is indistinguishable from success and
    the only symptom is a missing output file. whitebox_tools.exe does crash: under a process
    pool it panics (exit code 101) on a minority of runs, and without the child's real exit
    status there is nothing to tell that apart from a bad input.
    """
    import whitebox.whitebox_tools as whitebox_module

    output = _WhiteboxOutput()
    launched = []
    original_popen = whitebox_module.Popen

    def recording_popen(*popen_args, **popen_kwargs):
        process = original_popen(*popen_args, **popen_kwargs)
        launched.append(process)
        return process

    whitebox_module.Popen = recording_popen
    try:
        code = tool(*args, callback=output, **kwargs)
    finally:
        whitebox_module.Popen = original_popen

    if expected.exists():
        return code

    exit_codes = []
    for process in launched:
        try:
            exit_codes.append(process.wait(timeout=30))
        except Exception as exc:
            exit_codes.append(f"<{type(exc).__name__}>")
    reported = "\n".join(output.lines[-15:]) or "<the tool printed nothing>"
    panicked = " (101 is a Rust panic)" if 101 in exit_codes else ""
    raise FileNotFoundError(
        f"{description} was not created successfully: {expected}. "
        f"whitebox_tools exited with {exit_codes}{panicked}. Output:\n{reported}"
    )


def derive_hydrography_using_whitebox(workspace: Workspace, dem_raster: Raster, fixed_dem: np.ndarray, dem_for_conflation_path: Path) -> float:
    wbt = WhiteboxTools()
    wbt.set_compress_rasters(True)
    wbt.set_verbose_mode(LOG.level <= 20)  # INFO or lower
    wbt.set_max_procs(1)
    workspace.dem_updated_folder.mkdir(parents=True, exist_ok=True)
    workspace.Flow_Direction_Folder.mkdir(parents=True, exist_ok=True)

    attempts = 1
    while not workspace.filled_dem.exists() and attempts <= 1:
        _run_whitebox(wbt.fill_depressions, workspace.filled_dem,
                    f"Filled DEM from {dem_for_conflation_path}",
                    str(dem_for_conflation_path), str(workspace.filled_dem))
        attempts += 1
    _run_whitebox(wbt.d8_pointer, workspace.flowdir, "Flow direction file",
                  str(workspace.filled_dem), str(workspace.flowdir))
    _run_whitebox(wbt.d8_flow_accumulation, workspace.flowacc, "Flow accumulation file",
                  str(workspace.flowdir), str(workspace.flowacc), pntr=True,
                  out_type='catchment area')

    # If DEM units are not in km2, convert the threshold to the DEM units. This is important for the stream extraction step, which uses the flow accumulation raster to determine where streams are.
    threshold = workspace.configs.new_strm_threshold_km2
    threshold_native = threshold * dem_raster.native_cell_area / dem_raster.cell_area_km2
    buffer_distance = (50/1000) * np.sqrt(dem_raster.native_cell_area / dem_raster.cell_area_km2) # 50 m buffer distance in DEM units
    snap_distance = (0.1/1000) * np.sqrt(dem_raster.native_cell_area / dem_raster.cell_area_km2) # 0.1 m snap distance in DEM units

    # Remove the vector rasters, since whitebox will not make some of them if they exist
    for file in workspace.new_StrmShp.parent.glob("*"):
        if file.stem.startswith(workspace.new_StrmShp.stem):
            file.unlink()

    _run_whitebox(wbt.extract_streams, workspace.whitebox_stream_raster,
                  f"Whitebox stream raster (threshold {threshold_native} DEM units "
                  f"= {threshold} km2)",
                  str(workspace.flowacc), str(workspace.whitebox_stream_raster),
                  threshold=threshold_native, zero_background=True)

    # Remove flow accumulation for space
    workspace.flowacc.unlink()
            
    # Remove in the newly created stream raster streams where the dem == 0 (ocean) or where the dem is nodata. This is important for the stream extraction step, which uses the flow accumulation raster to determine where streams are.
    streams_ds: gdal.Dataset = gdal.Open(str(workspace.whitebox_stream_raster), gdal.GA_Update)
    streams = streams_ds.ReadAsArray()
    streams[fixed_dem == 0] = 0
    streams[fixed_dem == dem_raster.nodata_value] = 0
    streams_ds.WriteArray(streams)
    streams_ds = None

    wbt.raster_streams_to_vector(str(workspace.whitebox_stream_raster), str(workspace.flowdir), str(workspace.new_StrmShp), callback=whitebox_callback)
    if not workspace.new_StrmShp.exists():
        if workspace.configs.raise_errors_if_nothing_in_domain:
            raise FileNotFoundError(f"New stream shapefile {workspace.new_StrmShp} was not created successfully.")
        else:
            if workspace.new_StrmShp_matched.exists():
                workspace.new_StrmShp_matched.unlink()
            workspace.DEM_StrmShp = workspace.new_StrmShp_matched
            return

    wbt.repair_stream_vector_topology(str(workspace.new_StrmShp), str(workspace.new_StrmShp), dist=snap_distance, callback=whitebox_callback)
    wbt.run_tool(
        "vector_stream_network_analysis",
        [
            f"--streams='{workspace.new_StrmShp}'",
            f"--output='{workspace.new_StrmShp}'",
            f"--snap={snap_distance}",
        ],
        callback=whitebox_callback,
    )

    # Remove the other vectors that were created
    for file in workspace.new_StrmShp.parent.glob("*"):
        if file.stem.startswith(workspace.new_StrmShp.stem) and file.stem != workspace.new_StrmShp.stem:
            file.unlink()

    # Whitebox does not insert the projection into the shapefile, so we need to do that here.
    prj_file = workspace.new_StrmShp.with_suffix('.prj')
    with open(prj_file, 'w') as f:
        f.write(dem_raster.projection)

    return buffer_distance

def burn_streams_and_move_streams(workspace: Workspace) -> Path:
    """
    This function burns streams into the DEM and/or moves the streams to the correct location.
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

    if should_burn_streams:
        workspace.dem_updated_folder.mkdir(parents=True, exist_ok=True)

        lakes = load_lake_array(workspace, assigned_dem)

        source_gdf = Vector(workspace.DEM_StrmShp, not configs.parallel).to_geopandas().to_crs(assigned_dem.projection)
        channel_mask, dem_for_conflation = smooth_and_burn_dem(
            workspace, 
            source_gdf, 
            lakes,
            configs.streamflow_source.upstream_id, 
            configs.streamflow_source.downstream_id
        )
        dem_for_conflation_path = workspace.fixed_dem
    elif should_move_streams:
        if configs.burn_streams:
            dem_for_conflation = Raster(workspace.fixed_dem).read_array()
            dem_for_conflation_path = workspace.fixed_dem
        else:
            dem_for_conflation = assigned_dem.read_array()
            dem_for_conflation_path = workspace.assigned_dem

        channel_mask = Raster(workspace.bathy_water_mask).read_array()
        source_gdf = Vector(workspace.DEM_StrmShp, not configs.parallel).to_geopandas().to_crs(assigned_dem.projection)

    if should_move_streams:
        lakes_gdf = load_lake_gdf(workspace, assigned_dem)
        buffer_distance = derive_hydrography_using_whitebox(workspace, assigned_dem, dem_for_conflation, dem_for_conflation_path)

        streams_gdf = _conflate_streams(
            source_gdf=source_gdf, 
            streams_vector=workspace.new_StrmShp, 
            lakes_gdf=lakes_gdf,
            buffer_distance=buffer_distance,
            dem_proj=assigned_dem.projection, 
            dem_bbox=assigned_dem.bbox,
            source_id_col=configs.streamflow_source.upstream_id,
            source_ds_col=configs.streamflow_source.downstream_id,
            strm_order_col=configs.StrmOrder_Field,
            drop_multilinestrings=configs.drop_multilinestrings
        )
        if streams_gdf.empty:
            if configs.raise_errors_if_nothing_in_domain:
                raise ValueError("No stream geometries remain after conflation.")
            else:
                workspace.DEM_StrmShp = workspace.new_StrmShp_matched
                return None

        if configs.StrmOrder_Field and (configs.StrmOrder_Lower is not None or configs.StrmOrder_Upper is not None) and not configs.mapper.is_curve2flood_fldpln_mapper():
            streams_gdf = _filter_streams_by_stream_order(streams_gdf, configs.StrmOrder_Field, configs.StrmOrder_Lower, configs.StrmOrder_Upper)

        kwargs = {'index': False}
        if workspace.DEM_StrmShp.suffix.lower().endswith('.parquet'):
            kwargs['compression'] = 'brotli'
            kwargs['write_covering_bbox'] = True
            kwargs['geometry_encoding'] = 'geoarrow'

        Vector.save_any_geom(streams_gdf, workspace.new_StrmShp_matched, **kwargs)
        _rasterize_streams(str(workspace.new_stream_raster), str(dem_for_conflation_path), str(workspace.new_StrmShp_matched), attribute=configs.streamflow_source.upstream_id)

        final_streams = gdal.Open(str(workspace.new_stream_raster)).ReadAsArray()
        channel_mask |= (final_streams > 0)
        channel_mask = remove_cells_not_connected(channel_mask, final_streams)

        workspace.bathy_water_mask.parent.mkdir(parents=True, exist_ok=True)
        make_channel_mask(channel_mask, str(dem_for_conflation_path), str(workspace.bathy_water_mask), configs.compression)

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
        )

def smooth_and_burn_dem(
        workspace: Workspace, 
        source_gdf: gpd.GeoDataFrame,
        lakes: np.ndarray | None = None, 
        id_col: str = 'LINKNO', 
        ds_col: str = 'DSLINKNO') -> tuple[np.ndarray, np.ndarray]:
    dem_ds: gdal.Dataset = gdal.Open(workspace.assigned_dem)
    dem = dem_ds.ReadAsArray()
    nodata_value = dem_ds.GetRasterBand(1).GetNoDataValue()
    if nodata_value is None:
        nodata_value = -9999
    nan_mask = np.isnan(dem)
    dem[nan_mask] = nodata_value

    ocean_mask = (dem == 0)
    dem[ocean_mask] = nodata_value

    streams = _get_stream_raster(dem, dem_ds, workspace.DEM_StrmShp, id_col)

    if workspace.configs.use_dem_derived_channel_mask:
        channel_mask = (dem % 0.5 == 0)
    else:
        channel_mask = Raster(workspace.bathy_water_mask).read_array()

    channel_mask = burn_streams_into_dem(channel_mask, dem, streams, dem_ds, source_gdf, lakes, id_col, ds_col, nodata_value)
    dem = smooth_burned_dem(dem, channel_mask, streams, pbar=False)

    dem[dem < -1000] = nodata_value # Remove any DEM values that are less than -1000 m, since these are likely to be erroneous and will cause problems with the floodplain mapping.
    output_ds = gdal.GetDriverByName('GTiff').Create(workspace.fixed_dem, dem_ds.RasterXSize, dem_ds.RasterYSize, 1, gdal.GDT_Float32)
    output_ds.WriteArray(dem)
    output_ds.SetGeoTransform(dem_ds.GetGeoTransform())
    output_ds.SetProjection(dem_ds.GetProjection())
    output_ds.GetRasterBand(1).SetNoDataValue(nodata_value)
    output_ds = None

    return channel_mask, dem

def build_mask_graph(dem: np.ndarray, mask: np.ndarray) -> nx.Graph:
    G = nx.Graph()

    rows, cols = np.nonzero(mask)

    G.add_nodes_from(
        ((r, c), {"elevation": dem[r, c]})
        for r, c in zip(rows, cols)
    )

    # Horizontal
    rr, cc = np.nonzero(mask[:, :-1] & mask[:, 1:])
    G.add_edges_from(zip(zip(rr, cc), zip(rr, cc + 1)))

    # Vertical
    rr, cc = np.nonzero(mask[:-1, :] & mask[1:, :])
    G.add_edges_from(zip(zip(rr, cc), zip(rr + 1, cc)))

    # Diagonal
    rr, cc = np.nonzero(mask[:-1, :-1] & mask[1:, 1:])
    G.add_edges_from(zip(zip(rr, cc), zip(rr + 1, cc + 1)))

    return G


def elevation_components_nx(G: nx.Graph, elevations: dict) -> list[set]:
    visited = set()
    components = []

    # Traverse graph, finding connected components of nodes with the same elevation. Each component is a set of (row, col) tuples.
    for node in G.nodes:
        if node in visited:
            continue

        elev = elevations[node]
        stack = [node]
        component = set()

        while stack:
            current = stack.pop()
            if current in visited:
                continue

            visited.add(current)
            component.add(current)

            for neighbor in G.neighbors(current):
                if neighbor not in visited and elevations[neighbor] == elev:
                        stack.append(neighbor)

        components.append(component)

    return components

def smooth_burned_dem(dem: np.ndarray, mask: np.ndarray = None, streams: np.ndarray = None, pbar: bool = True, max_difference: float = 0.5) -> np.ndarray:
    dem = dem.astype(np.float32, copy=False)
    if mask is None:
        mask = (dem % max_difference) == 0

    banks = binary_dilation(mask, structure=np.ones((3, 3), dtype=int)).astype(np.uint8, copy=False)
    banks[mask] = False  # Banks are the cells adjacent to the mask, but not in the mask

    # Get a distance raster for the mask, where the distance is highest in the center, but smallest at the edges
    distance_to_banks = distance_transform_edt(mask)

    G = build_mask_graph(dem, mask)
    elevations = nx.get_node_attributes(G, "elevation")
    components = elevation_components_nx(G, elevations)
    final_node_elevations = {}
    for component in tqdm.tqdm(components, desc="Processing elevation components", disable=not pbar):
        if len(component) <= 1:
            continue

        # 2: Find all nodes that are on the outside edge of the component (a node that outside component but has a neighbor inside the component, but within the mask)
        boundary_nodes = set()
        max_distance_to_banks = 0
        min_distance_to_banks = float('inf')
        for node in component:
            for neighbor in G.neighbors(node):
                if neighbor not in component:
                    boundary_nodes.add(neighbor)
                    if distance_to_banks[neighbor] > max_distance_to_banks:
                        max_distance_to_banks = distance_to_banks[neighbor]
                    if distance_to_banks[neighbor] < min_distance_to_banks:
                        min_distance_to_banks = distance_to_banks[neighbor]

        # 3: Starting at the boundary, encroach into the component, and for each node, compute a new elevation
        # as the number of component nodes encroached over the total nodes times the difference between the boundary elevation and the starting elevation.
        if not boundary_nodes:
            continue

        modified_node_elevations = {}
        source_elevation = elevations[list(component)[0]]

        # 3.1 Find all nodes in the component that are attatched to a boundary node
        visited = set()
        global_index = 0
        while boundary_nodes:
            new_boundary = set()
            for node in boundary_nodes:
                for neighbor in G.neighbors(node):
                    if neighbor in component:
                        if neighbor in visited:
                            continue
                        if global_index == 0 and abs(elevations[node] - source_elevation) > max_difference:
                            continue
                        new_boundary.add(neighbor)
                        visited.add(neighbor)
                        if global_index == 0:
                            modified_node_elevations[neighbor] = (elevations[node], global_index)
                        else:
                            modified_node_elevations[neighbor] = (modified_node_elevations[node][0], global_index)

            global_index += 1
            boundary_nodes = new_boundary

        # Calculate the max index for each boundary elevation
        boundary_elevation_to_max_index = {}
        for node, (boundary_elevation, distance) in modified_node_elevations.items():
            if boundary_elevation not in boundary_elevation_to_max_index:
                boundary_elevation_to_max_index[boundary_elevation] = distance
            else:
                boundary_elevation_to_max_index[boundary_elevation] = max(boundary_elevation_to_max_index[boundary_elevation], distance)

        for node, (boundary_elevation, distance) in modified_node_elevations.items():
            local_index = boundary_elevation_to_max_index[boundary_elevation]
            if local_index == 0:
                continue
            else:
                # Interpolation, which does not exactly pass through neither the boundary nor the source elevation
                # It will bring us close to those values, which allows for a smoother transition rather than
                # two of the same elevations back to back across components or within a component across internal meeting boundary.
                new_elevation = boundary_elevation + ((source_elevation - boundary_elevation) / 2) + (source_elevation - boundary_elevation) * ((distance+1) / (local_index+2) / 2)
            
                # Lower elevation up to 0.1 m to help stream be in the center of the channel. ARC allows flat water detection up to 0.1 m
                if max_distance_to_banks != min_distance_to_banks:
                    new_elevation -= (0.1 * (max(min(distance_to_banks[node], max_distance_to_banks), min_distance_to_banks) - min_distance_to_banks) / (max_distance_to_banks - min_distance_to_banks))

            # Ensure that the new elevation does not exceed the banks, by raising the banks a bit
            dem_mask = (dem[node[0]-1:node[0]+2, node[1]-1:node[1]+2] <= new_elevation)
            rows, cols = np.nonzero(
                dem_mask & banks[node[0]-1:node[0]+2, node[1]-1:node[1]+2]
            )
            if len(rows) > 0:
                dem[rows + node[0]-1, cols + node[1]-1] = new_elevation + 0.5

            final_node_elevations[node] = new_elevation

    for node, new_elevation in final_node_elevations.items():
        dem[node] = new_elevation

    return dem

def _get_stream_raster(dem: np.ndarray, dem_ds: gdal.Dataset, streamlines: str, id_col: str = 'LINKNO'):
    # Rasterize streamlines on the fly
    mem_ds = gdal.GetDriverByName('MEM').Create('', dem.shape[1], dem.shape[0], 1, gdal.GDT_Int32)
    mem_ds.SetGeoTransform(dem_ds.GetGeoTransform())
    mem_ds.SetProjection(dem_ds.GetProjection())
    stream_ds = ogr.Open(streamlines)
    stream_layer = stream_ds.GetLayer()
    gdal.RasterizeLayer(mem_ds, [1], stream_layer, options=[f"ATTRIBUTE={id_col}"])
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
        min_feature_size: int = 5
):
    G = nx.from_pandas_edgelist(
        streams_gdf[streams_gdf[ds_col] > 0],
        source=id_col,
        target=ds_col,
        create_using=nx.DiGraph
    )
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

    # Mask out ocean (where elevation == 0)
    ocean_mask = (dem == 0)
    channel_mask &= ~ocean_mask

    # Mask out dem's no data value
    if nodata_value is not None:
        channel_mask &= (dem != nodata_value)

    labels, num_features = label(channel_mask, structure=structure)
    
    # Buffer the stream raster as a new mask
    channel_border = binary_dilation(streams > 0, structure=structure).astype(np.uint8, copy=False)
    channel_border &= (streams == 0) & (dem > -9998)

    streams_gdf = streams_gdf.set_index(id_col)

    masked_dem = np.where(channel_border, dem, np.inf)
    local_min = minimum_filter(masked_dem, size=3, mode="nearest")

    # Traverse each segment. Identify which end is up and downstream.
    # Then, take the upstream value. If it is not a multiple of 0.5, lower it to so.
    # Go downstream. Each cell is a multiple of 0.5, no higher than upstream elevation or the current cell elevation.
    for row in streams_gdf.itertuples():
        geom = row.geometry
        stream_id = row.Index
        if geom.geom_type == "MultiLineString":
            for line in geom.geoms:
                burn_linestring(dem, streams, dem_ds, line, stream_id, G, streams_gdf, channel_mask, labels, local_min, nodata_value)
        elif geom.geom_type == "LineString":
            burn_linestring(dem, streams, dem_ds, geom, stream_id, G, streams_gdf, channel_mask, labels, local_min, nodata_value)
        else:
            raise ValueError(f"Unsupported geometry type: {geom.geom_type}")
        
    return channel_mask

@njit(cache=True, nogil=True)
def nearest_stream_raster_pixel(
    streams_array: np.ndarray,
    start_row: int,
    start_col: int,
    linkno: int
):
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
        channel_mask: np.ndarray, 
        labels: np.ndarray,
        local_min: np.ndarray,
        nodata_value: float):
    if linkno not in G:
        return  # Skip if the linkno is not in the graph
    
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
            return  # No valid point found within DEM bounds
        
    if not (0 <= row2 < dem.shape[0] and 0 <= col2 < dem.shape[1] and dem[math.floor(row2), math.floor(col2)] != nodata_value):
        # Traverse the linestring in reverse to find the last point that is within the DEM bounds
        for x, y in np.flipud(coords):
            col, row = gdal.ApplyGeoTransform(inverse_transform, x, y)
            if 0 <= row < dem.shape[0] and 0 <= col < dem.shape[1] and dem[math.floor(row), math.floor(col)] != nodata_value:
                col2, row2 = col, row
                break
        else:
            return  # No valid point found within DEM bounds
        
    row1, col1 = math.floor(row1), math.floor(col1)
    row2, col2 = math.floor(row2), math.floor(col2)
    row1, col1 = nearest_stream_raster_pixel(streams, row1, col1, linkno)
    row2, col2 = nearest_stream_raster_pixel(streams, row2, col2, linkno)

    # Can't rely on elevation to determine upstream/downstream because the DEM may have been modified by previous burns. (unless no up or downstream nodes exist) Instead, use the topology of the stream network.
    upstream_nodes = list(G.predecessors(linkno))
    downstream_nodes = list(G.successors(linkno))
    if upstream_nodes:          
        # Check if the first point is an endpoint in the upstream node. If not, then the first point is downstream and we need to reverse the line.
        upstream_node = upstream_nodes[0]
        upstream_geom = streams_gdf.at[upstream_node, 'geometry']
        if not Point(x1, y1).touches(upstream_geom):
            coords = np.flipud(coords)
            col1, row1, col2, row2 = col2, row2, col1, row1  # Swap the coordinates as well
    elif downstream_nodes and any(G.successors(downstream_nodes[0])):  # Only check downstream if the downstream node has a successor (i.e., it is not an outlet)
        # Check if the last point is an endpoint in the downstream node. If not, then the last point is upstream and we need to reverse the line.
        downstream_node = downstream_nodes[0]
        downstream_geom = streams_gdf.at[downstream_node, 'geometry']
        if not Point(x2, y2).touches(downstream_geom):
            coords = np.flipud(coords)
            col1, row1, col2, row2 = col2, row2, col1, row1  # Swap the coordinates as well
    else:
        # Fallback to using elevation to determine upstream/downstream if no upstream or downstream nodes exist.
        elev1 = dem[row1, col1]
        elev2 = dem[row2, col2]
        if elev1 < elev2:
            # Reverse the line
            coords = np.flipud(coords)
            col1, row1, col2, row2 = col2, row2, col1, row1  # Swap the coordinates as well

    col2, row2 = col1, row1

    # Check if we have any upstream ids.
    upstream_ids = list(G.predecessors(linkno))
    last_elevation = np.inf
    for upstream_id in upstream_ids:
        us_row, us_col = nearest_stream_raster_pixel(streams, row1, col1, upstream_id)
        if 0 <= us_row < dem.shape[0] and 0 <= us_col < dem.shape[1] and dem[us_row, us_col] != nodata_value and dem[us_row, us_col] < last_elevation:
            last_elevation = dem[us_row, us_col]

    _burn_linestring(dem, streams, coords, linkno, channel_mask, labels, local_min, inverse_transform, row1, col1, last_elevation, nodata_value)

@njit(cache=True, nogil=True, parallel=True)
def _burn_linestring(
    dem: np.ndarray, 
    streams: np.ndarray, 
    coords: np.ndarray, 
    linkno: int, 
    channel_mask: np.ndarray, 
    labels: np.ndarray, 
    local_min: np.ndarray, 
    gt: tuple,
    row1: int, 
    col1: int, 
    last_elevation: float,
    nodata_value: float) -> None:
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
            rows.append(row)
            cols.append(col)
            last_row = row
            last_col = col

    started_in_mask = channel_mask[row1, col1]
    write = True
    will_come_back = True
    has_written = False

    # The trailing edge-of-DEM check below reads state from the last iteration of the loop.
    # A single-pixel reach never enters that loop, so these have to exist beforehand -- and
    # numba needs them bound on every path to compile the function at all. `has_written`
    # gates both uses, so the seed values are never the ones acted on.
    row2 = row1
    col2 = col1
    outstream_neighbor_count = 0

    for i, (row1, col1, row2, col2) in enumerate(zip(rows[:-1], cols[:-1], rows[1:], cols[1:])):
        if started_in_mask and write and not channel_mask[row2, col2]:
            if will_come_back:
                # Check if this linestring will eventually go back in the mask (and that point is connected to this one)
                for j in range(i + 1, len(rows)):
                    row3, col3 = rows[j], cols[j]
                    if channel_mask[row3, col3] and labels[row1, col1] == labels[row3, col3]:
                        # We can stop burning because we know we will come back (the source stream wandered away slightly from DEM channel)
                        write = False
                        break
                else:
                    will_come_back = False
        elif started_in_mask and not write and channel_mask[row2, col2]:
            write = True
        elif not started_in_mask and channel_mask[row2, col2]:
            started_in_mask = True

        if not write:
            continue

        has_written = True

        upstream_elev = dem[row1, col1]
        downstream_elev = dem[row2, col2]

        if upstream_elev > last_elevation:
            upstream_elev = last_elevation

        min_non_stream_elev = local_min[row1, col1]
        if upstream_elev > min_non_stream_elev:
            upstream_elev = min_non_stream_elev

        if upstream_elev % 0.5 != 0:
            upstream_elev = math.floor(upstream_elev * 2) / 2

        dem[row1, col1] = upstream_elev

        if downstream_elev > upstream_elev:
            downstream_elev = upstream_elev
        elif downstream_elev % 0.5 != 0:
            downstream_elev = math.floor(downstream_elev * 2) / 2

        dem[row2, col2] = downstream_elev
        last_elevation = downstream_elev

        # Suppose that there are two+ neighbors, with an elevation <= new_elevation.
        # If one is in the stream raster but the other is not, let us bump the elevation of the other up
        minr = max(0, row1 - 1)
        maxr = min(nrows - 1, row1 + 1)
        minc = max(0, col1 - 1)
        maxc = min(ncols - 1, col1 + 1)
        rs, cs = np.nonzero(dem[minr:maxr+1, minc:maxc+1] <= upstream_elev)
        rs += minr
        cs += minc
        instream_neighbors = []
        outstream_neighbors = []
        for r, c in zip(rs, cs):
            if (r == row1 and c == col1) or dem[r, c] == nodata_value:
                continue
            if streams[r, c] > 0:
                instream_neighbors.append((r, c))
            else:
                outstream_neighbors.append((r, c))

        outstream_neighbor_count = len(outstream_neighbors)

        if instream_neighbors and outstream_neighbors:
            for r, c in outstream_neighbors:
                dem[r, c] = upstream_elev + 0.5

    # One more thing: check if the last (row2, col2) is on the border of the dem. If so, drop by 0.5 (helps filled dem step route out of the DEM)
    if has_written and (row2 == 0 or row2 == nrows - 1 or col2 == 0 or col2 == ncols - 1) and dem[row2, col2] != nodata_value:
        dem[row2, col2] -= 0.5
    # Same check, but for if we are on the edge of nodata
    elif has_written and outstream_neighbor_count == 0 and dem[row2, col2] != nodata_value:
        dem[row2, col2] -= 0.5

class NodeType(Enum):
    ISOLATED = 1
    OUTLET = 2
    SOURCE = 3
    MERGED_SOURCE = 4
    CONFLUENCE = 5
    INTERIOR = 6

    def is_outlet(self) -> bool:
        return self == NodeType.OUTLET or self == NodeType.ISOLATED
    
    def is_source(self) -> bool:
        return self == NodeType.SOURCE or self == NodeType.MERGED_SOURCE or self == NodeType.ISOLATED


@dataclass
class ReachSig:
    geom: LineString
    buffer_geom: Polygon
    centroid: Point
    ancestors: set
    kind: NodeType                      # outlet, confluence, source, interior
    local_order: int

def build_graph(gdf: gpd.GeoDataFrame, id_col: str, ds_col: str) -> nx.DiGraph:
    """
    Build a downstream-directed DAG where each graph node is a stream reach.
    Node attributes store the reach geometry and derived properties.
    The downstream reach is represented by a directed edge: upstream_reach -> downstream_reach.
    """
    G = nx.DiGraph()

    if gdf.empty:
        return G

    if "geometry" not in gdf.columns:
        raise ValueError("GeoDataFrame must have a geometry column.")

    for row in gdf.itertuples(index=False):
        rid = getattr(row, id_col)
        geom = getattr(row, "geometry")
        buffered = getattr(row, "buffered")
        if geom is None or geom.is_empty:
            continue

        if isinstance(geom, GeometryCollection):
            geom = max((g for g in geom.geoms if isinstance(g, LineString)), key=lambda x: x.length, default=None)
            if geom is None:
                continue

        multi_geom = None

        if not isinstance(geom, LineString):
            # if a MultiLineString slipped through, merge if possible
            if isinstance(geom, MultiLineString):
                multi_geom = geom
                merged = line_merge(geom)
                if isinstance(merged, LineString):
                    geom = merged
                elif isinstance(merged, MultiLineString):
                    # The downstream is the geom which tocuhes the gdf, without this id in it
                    touching = gdf[(gdf[id_col] != rid) & gdf.touches(merged)]
                    if touching.empty:
                        continue
                    downstream = touching[
                        ~touching[ds_col].isin(touching[id_col])
                    ]
                    geoms = [g for g in merged.geoms if g.geom_type == "LineString"]
                    downstream_geom = [g for g in geoms if any(g.touches(d) for d in downstream.geometry)]
                    if downstream_geom:
                        geom = downstream_geom[0] # We want this geometry to match with wtbx
                    else:
                        # Just use the longest geom if no downstream geom is found
                        geom = max(geoms, key=lambda x: x.length)
                else:
                    continue
            else:
                continue

        ds_id = getattr(row, ds_col, None)
        if pd_is_na(ds_id):
            ds_id = 0

        G.add_node(
            rid,
            geometry=geom,
            multi_geom=multi_geom,
            centroid=geom.centroid,
            STRAHLER=getattr(row, "STRAHLER", None),
            buffered=buffered
        )

    for row in gdf.itertuples(index=False):
        rid = getattr(row, id_col)
        ds_id = getattr(row, ds_col, None)
        if pd_is_na(ds_id):
            continue
        try:
            ds_id = int(ds_id)
        except Exception:
            # allow string ids too
            pass
        if ds_id in (0, -1, None):
            continue
        if rid in G and ds_id in G:
            G.add_edge(rid, ds_id)

    return G


# ---------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------

def compute_reach_signatures(G: nx.DiGraph) -> dict[int, ReachSig]:
    """
    In this graph representation, every graph node is a reach.
    So the reach signature is attached to each node id.
    """
    sig: dict[int, ReachSig] = {}
    for n, data in G.nodes(data=True):
        geom = data["geometry"]
        prepare(geom)
        sig[n] = ReachSig(
            geom=geom,
            buffer_geom=data['buffered'],
            centroid=data['centroid'],
            ancestors=nx.ancestors(G, n) | {n},
            kind=classify_node(G, n, data['STRAHLER']),
            local_order=data['STRAHLER'],
        )
    return sig

def classify_node(G: nx.DiGraph, n: int, local_order: int) -> NodeType:
    indeg = G.in_degree(n)
    outdeg = G.out_degree(n)
    if indeg == 0 and outdeg == 0:
        return NodeType.ISOLATED
    if outdeg == 0:
        return NodeType.OUTLET
    if indeg == 0:
        if local_order > 1:
            return NodeType.MERGED_SOURCE
        return NodeType.SOURCE
    if indeg > 1:
        return NodeType.CONFLUENCE
    return NodeType.INTERIOR

def pd_is_na(x) -> bool:
    try:
        return bool(x is None or (isinstance(x, float) and np.isnan(x)))
    except Exception:
        return x is None

def is_linear_reach(G: nx.DiGraph, upstream: int, downstream: int):
    path = nx.shortest_path(G, upstream, downstream)

    for node in path[1:]:
        if G.in_degree(node) != 1:
            return False

    return True

def split_network_at_confluences(
    wtbx_gdf: gpd.GeoDataFrame, 
    source_gdf: gpd.GeoDataFrame,
    buffer_distance: float, 
    wtbx_id_col: str, 
    wtbx_ds_col: str):
    confluence_points = set()

    for geom in source_gdf.geometry:
        geom = line_merge(geom)
        if isinstance(geom, LineString):
            coords = geom.coords
            confluence_points.add(Point(coords[0]))
            confluence_points.add(Point(coords[-1]))

    # If the confluence point is within the buffer distance, let us split the wtbx stream at that point and create a new reach. This will help with matching the streams better.
    max_fid = wtbx_gdf[wtbx_id_col].max()
    geom_col_index = wtbx_gdf.columns.to_list().index('geometry')
    ds_col_index = wtbx_gdf.columns.to_list().index(wtbx_ds_col)

    for confluence_point in confluence_points:
        candidate_idx = wtbx_gdf.sindex.query(
            confluence_point,
            predicate="dwithin",
            distance=buffer_distance,
        )

        if len(candidate_idx) != 1:
            continue

        idx = candidate_idx[0]
        row = wtbx_gdf.iloc[idx]

        # Split the line
        wtbx_river: LineString = row.geometry

        d = wtbx_river.project(confluence_point)

        # This is the correct order, since whitebox always makes streamlines from upstream to downstream, so the first part of the split is always upstream and the second part is always downstream.
        new_upstream = substring(wtbx_river, 0, d)
        new_downstream = substring(wtbx_river, d, wtbx_river.length)


        # Let's not split if it makes a super tiny river segment.
        if min(new_upstream.length, new_downstream.length) < buffer_distance:
            continue

        max_fid += 1
        new_fid = max_fid

        # Create a new row for the downstream segment
        new_row = row.copy()
        new_row[wtbx_id_col] = new_fid
        new_row['geometry'] = new_downstream

        # I know this is not the most efficient way but it works and idk how to do it faster
        wtbx_gdf = pd.concat([wtbx_gdf, pd.DataFrame([new_row])], ignore_index=True)
        wtbx_gdf.iat[idx, geom_col_index] = new_upstream
        wtbx_gdf.iat[idx, ds_col_index] = new_fid

    return wtbx_gdf

def enforce_endorheic_basins(
        source_gdf: gpd.GeoDataFrame,
        wtbx_gdf: gpd.GeoDataFrame,
        source_id_col: str,
        source_ds_col: str,
        wtbx_id_col: str,
        wtbx_ds_col: str,
):
    GA: nx.DiGraph = nx.from_pandas_edgelist(source_gdf[source_gdf[source_ds_col] > 0], source=source_id_col, target=source_ds_col, create_using=nx.DiGraph())
    GB: nx.DiGraph = nx.from_pandas_edgelist(wtbx_gdf[wtbx_gdf[wtbx_ds_col] > 0], source=wtbx_id_col, target=wtbx_ds_col, create_using=nx.DiGraph())
    GB.add_nodes_from(wtbx_gdf[wtbx_id_col])

    wtbx_outlets = {n for n in GB.nodes() if GB.out_degree(n) == 0}
    source_outlets = {n for n in GA.nodes() if GA.out_degree(n) == 0 or (GA.out_degree(n) == 1 and max(GA.in_degree(d) for d in nx.descendants(GA, n)) <= 1)} # Allow matching to upstream of outlet that are on single line
    source_outlets.update(source_gdf[~source_gdf[source_id_col].isin(source_outlets) & (source_gdf[source_ds_col] < 0)][source_id_col].tolist()) # Single headwaters can be matched now

    wtbx_outlets_to_remove = set()
    for wtbx_outlet in wtbx_outlets:
        wtbx_outlet_geom = wtbx_gdf.loc[wtbx_gdf[wtbx_id_col] == wtbx_outlet, 'geometry'].values[0]
        possible_matches_index = list(source_gdf.sindex.intersection(wtbx_outlet_geom.bounds))
        candidates = source_gdf.iloc[possible_matches_index]
        # Add to the candidates, up to 3 downstream of the candidate nodes, since sometimes the wtbx outlet is upstream of the source outlet, but the source outlet is not in the AOI. So we need to look downstream only a little to find a match
        for _ in range(3):
            next_candidates = source_gdf[source_gdf[source_id_col].isin(candidates[source_ds_col].tolist())]
            if next_candidates.empty:
                break
            candidates = pd.concat([candidates, next_candidates], ignore_index=True)
        candidates = candidates.drop_duplicates(subset=[source_id_col])
        candidates = candidates[candidates[source_id_col].isin(source_outlets)]  # Only consider source outlets
        if not candidates.empty:
            continue

        # Let's remove this wtbx outlet and all its ancestors
        ancestors = nx.ancestors(GB, wtbx_outlet)
        GB.remove_nodes_from(ancestors | {wtbx_outlet})
        wtbx_gdf = wtbx_gdf[~wtbx_gdf[wtbx_id_col].isin(ancestors | {wtbx_outlet})]
        wtbx_outlets_to_remove.add(wtbx_outlet)

    wtbx_outlets -= wtbx_outlets_to_remove

    source_outlets = {n for n in GA.nodes() if GA.out_degree(n) == 0} # Be more restrictive for this set
    # Now we do the same for the source outlets
    for source_outlet in source_outlets:
        if not any(source_gdf[source_id_col] == source_outlet):
            continue

        source_outlet_geom = source_gdf.loc[source_gdf[source_id_col] == source_outlet, 'geometry'].values[0]
        possible_matches_index = list(wtbx_gdf.sindex.intersection(source_outlet_geom.bounds))
        candidates = wtbx_gdf.iloc[possible_matches_index]
        candidates = candidates[candidates[wtbx_id_col].isin(wtbx_outlets)]  # Only consider wtbx outlets
        if not candidates.empty:
            continue

        # Find the nearest wtbx stream, remove it. Update its upstream's dslinkno to be -1
        candidates = wtbx_gdf.iloc[possible_matches_index]
        candidates = candidates.loc[candidates.geometry.distance(source_outlet_geom).sort_values().index]
        if candidates.empty:
            continue

        # Now, choose the nearest candidate, but if another candidate is more downstream, chose that (recursively)
        nearest_candidate = candidates.iloc[0]
        nearest_candidate_id = nearest_candidate[wtbx_id_col]
        downstream_id = next(GB.successors(nearest_candidate_id), None)
        while downstream_id:
            if downstream_id in candidates[wtbx_id_col].values:
                nearest_candidate_id = downstream_id
                downstream_id = next(GB.successors(nearest_candidate_id), None)
            else:
                break

        # Now, remove the nearest candidate and update its upstream's dslinkno to be -1
        upstream_mask = wtbx_gdf[wtbx_ds_col] == nearest_candidate_id
        if upstream_mask.any():
            wtbx_gdf.loc[wtbx_gdf[wtbx_ds_col] == nearest_candidate_id, wtbx_ds_col] = -1
        wtbx_gdf = wtbx_gdf[wtbx_gdf[wtbx_id_col] != nearest_candidate_id]
        GB.remove_node(nearest_candidate_id)   

    return wtbx_gdf

def build_graphs(
    source_gdf: gpd.GeoDataFrame,
    wtbx_gdf: gpd.GeoDataFrame,
    min_strm_order: int,
    source_id_col: str = "LINKNO",
    source_ds_col: str = "DSLINKNO",
    wtbx_id_col: str = "FID",
    wtbx_ds_col: str = "DS_LINK_ID",
):
    GA = build_graph(source_gdf, id_col=source_id_col, ds_col=source_ds_col)
    GB = build_graph(wtbx_gdf, id_col=wtbx_id_col, ds_col=wtbx_ds_col)

    assert nx.is_directed_acyclic_graph(GA), "GA must be a DAG."
    assert nx.is_directed_acyclic_graph(GB), "GB must be a DAG."

    for node in nx.topological_sort(GA):
        orders = [
            GA.nodes[p]['STRAHLER']
            for p in GA.predecessors(node)
        ]

        if len(orders) == 0:
            GA.nodes[node]['STRAHLER'] = min_strm_order # The source gdf may have headwater streams merged, so there may not be a stream order 1. I assume that we will always have a headwater stream in the AOI for this to work.
            continue

        max_order = max(orders)

        if orders.count(max_order) >= 2:
            GA.nodes[node]['STRAHLER'] = max_order + 1
        else:
            GA.nodes[node]['STRAHLER'] = max_order

    for i, node in enumerate(nx.topological_sort(GB)):
        GB.nodes[node]['topological_order'] = i

    return GA, GB

def match_to_source_headwaters(
    best_source_scores: dict[int, tuple[int, int]],
    scores: dict[int, float],
    source_headwaters: set[int],
    wtbx_headwaters: set[int],
    A_reach_sig: dict[int, ReachSig],
    B_reach_sig: dict[int, ReachSig],
    wtbx_gdf: gpd.GeoDataFrame,
    wtbx_id_col: str,
    min_strm_order: int,
    GB: nx.DiGraph
):
    wtbx_potential_headwaters = {
        n for n, sig in B_reach_sig.items() if sig.local_order == min_strm_order and all(B_reach_sig[pred].local_order < min_strm_order for pred in GB.predecessors(n)) # The predecessors are both headwaters
    }
    wtbx_potential_headwaters -= wtbx_headwaters

    for source_headwater in source_headwaters:
        best_score = 0
        best_wtbx = None
        source_buffer = A_reach_sig[source_headwater].buffer_geom

        nearest_wtbx_headwaters = wtbx_gdf.iloc[wtbx_gdf.sindex.query(source_buffer, predicate="intersects", sort=True)][wtbx_id_col]
        nearest_wtbx_headwaters = nearest_wtbx_headwaters[nearest_wtbx_headwaters.isin(wtbx_headwaters)].tolist()

        for wtbx_outlet in nearest_wtbx_headwaters:
            wtbx_buffer = B_reach_sig[wtbx_outlet].buffer_geom
            intersection_area = source_buffer.intersection(wtbx_buffer).area
            score = intersection_area

            if score > best_score:
                best_score = score
                best_wtbx = wtbx_outlet

        nearest_wtbx_potential_headwaters = wtbx_gdf.iloc[wtbx_gdf.sindex.query(source_buffer, predicate="intersects", sort=True)][wtbx_id_col]
        nearest_wtbx_potential_headwaters = nearest_wtbx_potential_headwaters[nearest_wtbx_potential_headwaters.isin(wtbx_potential_headwaters)].tolist()

        for wtbx_outlet in nearest_wtbx_potential_headwaters:
            wtbx_buffer = B_reach_sig[wtbx_outlet].buffer_geom
            intersection_area = source_buffer.intersection(wtbx_buffer).area
            score = intersection_area
            if score > best_score:
                best_score = score
                best_wtbx = wtbx_outlet

        if best_wtbx is not None and best_score > scores.get(best_wtbx, 0):
            best_source_scores[source_headwater] = (best_wtbx, source_headwater)
            scores[best_wtbx] = best_score

def match_others_to_source_headwaters(
    best_source_scores: dict[int, tuple[int, int]],
    scores: dict[int, float],
    source_gdf: gpd.GeoDataFrame,
    A_reach_sig: dict[int, ReachSig],
    B_reach_sig: dict[int, ReachSig],
    non_source_headwaters: set[int],
    source_id_col: str,
    GA: nx.DiGraph
):
    # Check the computed wtbx headwaters, and see if there is a better match for any of them. If so, replace the match.
    for source_headwater, (wtbx_headwater, _) in list(best_source_scores.items()):
        best_score = scores.get(wtbx_headwater, 0)
        best_source = None
        wtbx_buffer = B_reach_sig[wtbx_headwater].buffer_geom

        nearest_source_headwaters = source_gdf.iloc[source_gdf.sindex.query(wtbx_buffer, predicate="intersects", sort=True)][source_id_col]
        nearest_source_headwaters = nearest_source_headwaters[nearest_source_headwaters.isin(non_source_headwaters)].tolist()

        for source_headwater2 in nearest_source_headwaters:
            source_buffer2 = A_reach_sig[source_headwater2].buffer_geom
            intersection_area = source_buffer2.intersection(wtbx_buffer).area
            score = intersection_area

            if score > best_score:
                best_score = score
                best_source = source_headwater2

        if best_source is None or best_source == source_headwater:
            continue

        if not nx.has_path(GA, best_source, source_headwater) and nx.has_path(GA, source_headwater, best_source) and is_linear_reach(GA, source_headwater, best_source):
            # We matched better with a downstream segment, but let us actually keep the upstream one instead (in a linear reach, i.e., no upstream tributaries)
            continue

        del best_source_scores[source_headwater]
        best_source_scores[best_source] = (wtbx_headwater, best_source)

def match_wtbx_headwaters(
    best_source_scores: dict[int, tuple[int, int]],
    scores: dict[int, float],
    source_gdf: gpd.GeoDataFrame,
    A_reach_sig: dict[int, ReachSig],
    B_reach_sig: dict[int, ReachSig],
    non_source_headwaters: set[int],
    wtbx_headwaters: set[int],
    source_id_col: str,
    GB: nx.DiGraph,
    min_strm_order: int
):
    # One more check: let's look at any unassigned wtbx headwater and see if we have a really good match for it. If so, assign it.
    for wtbx_headwater in wtbx_headwaters:
        if wtbx_headwater in best_source_scores.values():
            continue

        wtbx_buffer = B_reach_sig[wtbx_headwater].buffer_geom
        best_score = wtbx_buffer.area * 0.33  # Only consider matches that are at least 33% of the wtbx headwater area
        best_source = None

        downstream_wtbx = next(iter(GB.successors(wtbx_headwater)), None)
        siblings = set(GB.predecessors(downstream_wtbx)) - {wtbx_headwater} if downstream_wtbx is not None else set()

        # Use sindex to find nearest source headwaters to the wtbx headwater centroid
        nearest_source_headwaters = source_gdf.iloc[source_gdf.sindex.query(wtbx_buffer, predicate="intersects", sort=True)][source_id_col]
        nearest_source_headwaters = nearest_source_headwaters[nearest_source_headwaters.isin(non_source_headwaters)].tolist()

        for source_headwater in nearest_source_headwaters:
            if A_reach_sig[source_headwater].local_order > min_strm_order:
                # We are going to be more strict here
                continue

            source_buffer = A_reach_sig[source_headwater].buffer_geom
            intersection_area = source_buffer.intersection(wtbx_buffer).area
            score = intersection_area

            if score <= best_score:
                continue

            # We have to be careful here. If the downstream segment or the sibling (other upstream of downstream) matches better, don't match
            if downstream_wtbx is not None:
                downstream_buffer = B_reach_sig[downstream_wtbx].buffer_geom
                downstream_intersection_area = source_buffer.intersection(downstream_buffer).area
                if downstream_intersection_area > score:
                    continue

                should_skip = False
                for sibling in siblings:
                    sibling_buffer = B_reach_sig[sibling].buffer_geom
                    sibling_intersection_area = source_buffer.intersection(sibling_buffer).area
                    if sibling_intersection_area > score:
                        should_skip = True
                        break
                    
                if should_skip:
                    continue

            best_score = score
            best_source = source_headwater

        if best_source is not None and best_score > scores.get(wtbx_headwater, 0):
            best_source_scores[best_source] = (wtbx_headwater, best_source)

def find_outlets_of_headwaters(
    best_source_scores: dict[int, tuple[int, int]],
    A_reach_sig: dict[int, ReachSig],
    B_reach_sig: dict[int, ReachSig],
    GA: nx.DiGraph,
    GB: nx.DiGraph
):
    headwater_fids = set(f for f, _ in best_source_scores.values())
    headwater_fid_to_linkno = {f: l for f, l in best_source_scores.values()}
    headwater_fids_accounted_for = set()
    best_outlet_linkno_to_fid = {}

    if not headwater_fids:
        return headwater_fids, best_outlet_linkno_to_fid, headwater_fid_to_linkno

    # Which weakly-connected component of the source network each reach belongs to. Two source
    # reaches in different components can never drain to a common downstream reach, so this is
    # what makes the convergence test below answerable.
    component_of_linkno = {}
    for component_index, component in enumerate(nx.weakly_connected_components(GA)):
        for linkno in component:
            component_of_linkno[linkno] = component_index

    headwater_fids_to_remove = set()
    
    # For each headwater, find the corresponding outlet
    for headwater_fid in headwater_fids:
        headwater_linkno = headwater_fid_to_linkno[headwater_fid]
        # Find the outlet for this headwater
        outlet_fid = None
        descendants = list(nx.descendants(GB, headwater_fid))
        if len(descendants) == 0:
            # This headwater is an outlet, so we can just use it as the outlet
            headwater_fids_accounted_for.add(headwater_fid)
            best_outlet_linkno_to_fid[headwater_linkno] = headwater_fid
            continue
        else:
            for fid in nx.descendants(GB, headwater_fid):
                if GB.out_degree(fid) == 0:
                    outlet_fid = fid
                    break

        if outlet_fid is None:
            # Remove these headwaters from the best_source_scores, since they don't have a corresponding outlet
            del best_source_scores[headwater_fid_to_linkno[headwater_fid]]
            continue

        # Find all headwater fids that connect to this outlet, with no current outlet
        outlet_headwaters = set(f for f in B_reach_sig[outlet_fid].ancestors) & headwater_fids
        outlet_headwaters -= headwater_fids_to_remove
        
        # Find the most common downstream linkno for these headwaters in GA
        headwater_linknos = [headwater_fid_to_linkno[f] for f in outlet_headwaters]

        # The Whitebox network comes off the filled DEM, so it drains the whole domain as one
        # connected system. The clipped source network does not -- reaches whose downstream
        # neighbour lies outside the AOI were cut loose, leaving several components. Every
        # headwater sharing this Whitebox outlet therefore lands in the list above, including
        # ones the source network can never route to the same reach. Those are guaranteed
        # misses: keeping them only drags the convergence ratio down, and once it falls under
        # the threshold this outlet is abandoned and every reach on the path to it is dropped
        # from the conflated network. At South Platte that is what removed the entire order-6
        # mainstem -- 12 of 12 reachable headwaters converged on the true outlet, but 4
        # headwaters from other components made it read as 12/16.
        home_component = component_of_linkno.get(headwater_linkno)
        headwater_linknos = [
            linkno for linkno in headwater_linknos
            if component_of_linkno.get(linkno) == home_component
        ]
        if not headwater_linknos:
            headwater_linknos = [headwater_linkno]

        # Walk down from this headwater rather than an arbitrary member of a set, so the path
        # taken does not depend on set iteration order. Any headwater in the component reaches
        # the same confluence, so this only fixes which one we start from.
        current_linkno_outlet = headwater_linkno
        found = False
        while True:
            try:
                current_linkno_outlet = next(GA.successors(current_linkno_outlet))
            except StopIteration:
                break

            if all(hdwtr in A_reach_sig[current_linkno_outlet].ancestors for hdwtr in headwater_linknos):
                found = True
                break

        if not found and (sum(hdwtr in A_reach_sig[current_linkno_outlet].ancestors for hdwtr in headwater_linknos) / len(headwater_linknos)) > 0.9:
            # Sometimes, the headwaters might not all connect to the same downstream segment, but if most of them do, we can still use that segment as the outlet
            # Find the outlet that first connects to the most headwaters
            best_linkno = None
            best_count = 0
            for linkno in nx.descendants(GA, headwater_fid_to_linkno[headwater_fid]):
                count = sum(hdwtr in A_reach_sig[linkno].ancestors for hdwtr in headwater_linknos)
                if count > best_count:
                    best_count = count
                    best_linkno = linkno


            if best_linkno is not None:
                current_linkno_outlet = best_linkno
                # Check: is the linkno outlet in the same area as the outlet fid? If not, we should skip this outlet, and remove this headwater FID
                # This occurs when the headwater FID has been incorrectly matched to an endorheic basin
                outlet_geom = B_reach_sig[outlet_fid].geom
                linkno_geom = A_reach_sig[current_linkno_outlet].geom
                if (max(outlet_geom.length, linkno_geom.length) * 2) < outlet_geom.distance(linkno_geom):
                    # The outlet and linkno are too far apart, so we should skip this outlet, and remove this headwater FID permanently
                    headwater_fids_to_remove.add(headwater_fid)
                    del best_source_scores[headwater_fid_to_linkno[headwater_fid]]
                    GB.remove_node(headwater_fid)
                    del B_reach_sig[headwater_fid]
                    continue

                # The geoms should be within at least 
                found = True

        if not found:
            # Remove these headwaters from the best_source_scores, since they don't have a corresponding outlet
            for headwater_fid in outlet_headwaters:
                try:
                    del best_source_scores[headwater_fid_to_linkno[headwater_fid]]
                except KeyError:
                    pass
            continue

        # A check: if we had only 1 headwater or there's still downstream segments, than we will have matched with the immediate downstream. But, we should instead match with the stream segment with the most overlap
        if len(outlet_headwaters) == 1 or GA.out_degree(current_linkno_outlet) > 0:
            headwater_fid = next(iter(outlet_headwaters))
            best_score = 0
            best_linkno = None
            fid_buffer = B_reach_sig[outlet_fid].buffer_geom

            for linkno in nx.descendants(GA, headwater_fid_to_linkno[headwater_fid]):
                linkno_buffer = A_reach_sig[linkno].buffer_geom
                intersection_area = fid_buffer.intersection(linkno_buffer).area
                score = intersection_area

                if score > best_score:
                    best_score = score
                    best_linkno = linkno

            if best_linkno is not None:
                current_linkno_outlet = best_linkno


        headwater_fids_accounted_for.update(outlet_headwaters)
        best_outlet_linkno_to_fid[current_linkno_outlet] = outlet_fid

    headwater_fids = headwater_fids_accounted_for
    headwater_fids -= headwater_fids_to_remove

    return headwater_fids, best_outlet_linkno_to_fid, headwater_fid_to_linkno

def fill_in_paths_between_headwaters_and_outlets(
    best_outlet_linkno_to_fid: dict[int, int],
    headwater_fids: set[int],
    headwater_fid_to_linkno: dict[int, int],
    A_reach_sig: dict[int, ReachSig],
    B_reach_sig: dict[int, ReachSig],
    GA: nx.DiGraph,
    GB: nx.DiGraph,
    final_matches: dict[int, int],
    linkno_to_fid: dict[int, set[int]]
):
    for outlet_linkno, outlet_fid in best_outlet_linkno_to_fid.items():
        allowable_linknos = defaultdict(set)
        watershed_fids = set(B_reach_sig[outlet_fid].ancestors) | {outlet_fid}
        # Find common downstream segment for each headwater
        headwaters_of_outlet_that_are_assigned = watershed_fids & headwater_fids
        for fid_source_1, fid_source_2 in combinations(headwaters_of_outlet_that_are_assigned, 2):
            if fid_source_1 in { 604, 570} or fid_source_2 in { 604, 570}:
                pass
            # Find the first fid which has both as upstream.
            # Do the same for linkno
            confluence_fid = fid_source_1
            while True:
                if fid_source_2 in B_reach_sig[confluence_fid].ancestors:
                    break
                try:
                    confluence_fid = next(GB.successors(confluence_fid))
                except StopIteration:
                    break

            confluence_linkno = headwater_fid_to_linkno[fid_source_1]
            do_these_headwaters_connect = True
            while True:
                if headwater_fid_to_linkno[fid_source_2] in A_reach_sig[confluence_linkno].ancestors:
                    break
                try:
                    confluence_linkno = next(GA.successors(confluence_linkno))
                except StopIteration:
                    do_these_headwaters_connect = False
                    break

            if not do_these_headwaters_connect:
                continue

            if confluence_fid in final_matches and final_matches[confluence_fid] != confluence_linkno:
                # Choose the most downstream linkno
                if confluence_linkno in A_reach_sig[final_matches[confluence_fid]].ancestors:
                    confluence_linkno = final_matches[confluence_fid]

            if confluence_linkno in linkno_to_fid:
                # Fill all the fids between the linkno and the outlet with the same linkno
                other_fids = linkno_to_fid[confluence_linkno]
                to_add = set()
                for fid in other_fids:
                    if confluence_fid in B_reach_sig[fid].ancestors:
                        for fid in nx.shortest_path(GB, confluence_fid, fid):
                            final_matches[fid] = confluence_linkno
                            to_add.add(fid)
                    elif fid in B_reach_sig[confluence_fid].ancestors:
                        for fid in nx.shortest_path(GB, fid, confluence_fid):
                            final_matches[fid] = confluence_linkno
                            to_add.add(fid)
                    else:
                        continue

                linkno_to_fid[confluence_linkno].update(to_add)

            final_matches[confluence_fid] = confluence_linkno
            linkno_to_fid[confluence_linkno].add(confluence_fid)

            source1_path = set(nx.shortest_path(GA, headwater_fid_to_linkno[fid_source_1], confluence_linkno))
            for fid in nx.shortest_path(GB, fid_source_1, confluence_fid):
                allowable_linknos[fid].update(source1_path)

            source2_path = set(nx.shortest_path(GA, headwater_fid_to_linkno[fid_source_2], confluence_linkno))
            for fid in nx.shortest_path(GB, fid_source_2, confluence_fid):
                allowable_linknos[fid].update(source2_path)

            try:
                source3_path = set(nx.shortest_path(GA, confluence_linkno, outlet_linkno))
            except nx.NetworkXNoPath:
                source3_path = set() # Occurs if outlet_fid == confluence_fid
            for fid in nx.shortest_path(GB, confluence_fid, outlet_fid):
                allowable_linknos[fid].update(source3_path)

        if len(headwaters_of_outlet_that_are_assigned) == 1:
            source_path = set(nx.shortest_path(GA, headwater_fid_to_linkno[next(iter(headwaters_of_outlet_that_are_assigned))], outlet_linkno))
            for fid in nx.shortest_path(GB, next(iter(headwaters_of_outlet_that_are_assigned)), outlet_fid):
                allowable_linknos[fid].update(source_path)

        for fid, allowable in allowable_linknos.items():
            if fid in final_matches:
                continue

            new_allowable = []
            for linkno in allowable:
                existing_fids = linkno_to_fid[linkno]
                # Only add if the existing fids are immediately up or downstream of this fid
                if existing_fids:
                    S = GB.subgraph(existing_fids | {fid})
                    if not nx.is_weakly_connected(S):
                        continue

                    # Ensure in and out degree all <= 1
                    if any(S.in_degree(n) > 1 or S.out_degree(n) > 1 for n in S.nodes):
                        continue
                    
                new_allowable.append(linkno)

            allowable = new_allowable
            allowable = sorted(allowable, key=lambda x: A_reach_sig[x].geom.distance(B_reach_sig[fid].geom))[:4]

            fid_geom = B_reach_sig[fid].geom
            fid_buffer = B_reach_sig[fid].buffer_geom
            best_score = 0
            best_linkno = None
            for linkno in allowable:
                linkno_buffer = A_reach_sig[linkno].buffer_geom
                score = fid_buffer.intersection(linkno_buffer).area
                if score > best_score:
                    best_score = score
                    best_linkno = linkno

            if best_linkno is None:
                # Use hausdorff distance to find the closest linkno
                fid_geom = B_reach_sig[fid].geom
                best_score = np.inf
                for linkno in allowable:
                    linkno_geom = A_reach_sig[linkno].geom
                    score = fid_geom.hausdorff_distance(linkno_geom)
                    if score < best_score:
                        best_score = score
                        best_linkno = linkno

            # if best_linkno is still None, we need to stop processing and not pass this stream to the final matches.
            if best_linkno is None:
                continue

            final_matches[fid] = best_linkno
            linkno_to_fid[best_linkno].add(fid)

def fill_in_missing_branches(
    final_matches: dict[int, int],
    linkno_to_fid: dict[int, set[int]],
    A_reach_sig: dict[int, ReachSig],
    B_reach_sig: dict[int, ReachSig],
    GB: nx.DiGraph,
    source_gdf: gpd.GeoDataFrame,
    source_id_col: str
):
    # Ok, now check for any fids that have  descendants assigned, but they themselves are not assigned.
    # This represents headwater-type segments that were not caught, but still can be matched well
    for fid in B_reach_sig:
        if fid in final_matches:
            continue

        descendants = nx.descendants(GB, fid)
        if not any(d in final_matches for d in descendants):
            continue

        # First off, do we intersect more than 50% with any source stream?
        fid_buffer = B_reach_sig[fid].buffer_geom
        best_score = fid_buffer.area * 0.5
        best_linkno = None
        sindex = source_gdf.sindex
        possible_matches_index = list(sindex.intersection(fid_buffer.bounds))
        for idx in possible_matches_index:
            source_linkno = source_gdf.iloc[idx][source_id_col]
            if source_linkno not in A_reach_sig:
                continue

            source_buffer = A_reach_sig[source_linkno].buffer_geom
            intersection_area = fid_buffer.intersection(source_buffer).area
            score = intersection_area
            if score > best_score:
                best_score = score
                best_linkno = source_linkno

        if best_linkno is None:
            continue

        # Ensure that this linkno is either upstream of the same linkno of the linkno of the first matched descenant fid
        descendant_fid = next(GB.successors(fid))
        while descendant_fid not in final_matches:
            descendant_fid = next(GB.successors(descendant_fid))
        descendant_linkno = final_matches[descendant_fid]
        if not (best_linkno in A_reach_sig[descendant_linkno].ancestors or best_linkno == descendant_linkno):
            continue

        if linkno_to_fid[best_linkno]:
            S = GB.subgraph(linkno_to_fid[best_linkno] | {fid})
            if not nx.is_weakly_connected(S):
                continue

            # Ensure in and out degree all <= 1
            if any(S.in_degree(n) > 1 or S.out_degree(n) > 1 for n in S.nodes):
                continue

        final_matches[fid] = best_linkno
        linkno_to_fid[best_linkno].add(fid)

def update_wtbx_gdf(
    wtbx_gdf: gpd.GeoDataFrame,
    final_matches: dict[int, int],
    source_id_col: str,
    source_ds_col: str,
    dem_bbox: tuple[float, float, float, float],
    buffer_distance: float,
    GB: nx.DiGraph,
    source_gdf: gpd.GeoDataFrame,
    strm_order_col: str,
    dem_proj: str,
    drop_multilinestrings: bool

):
     # Finally assign to the gdf
    wtbx_gdf[source_id_col] = wtbx_gdf['FID'].map(lambda x: final_matches.get(x, np.nan))
    wtbx_gdf = wtbx_gdf[wtbx_gdf[source_id_col].notna()].copy()

    min_bounds = (
        dem_bbox[0] + buffer_distance,
        dem_bbox[1] + buffer_distance,
        dem_bbox[2] - buffer_distance,
        dem_bbox[3] - buffer_distance
    )
    valid_rows = (~wtbx_gdf.geometry.intersects(box(*min_bounds).boundary) | wtbx_gdf['FID'].apply(lambda x: GB.out_degree(x) == 0))
    if not valid_rows.any():
        wtbx_gdf = wtbx_gdf[valid_rows].copy() # Remove streams that touch near the border of the AOI, since they are likely to be the most incorrect. But keep outlets, since they are likely to be correct.
    wtbx_gdf['topological_order'] = wtbx_gdf['FID'].map(lambda x: GB.nodes[x]['topological_order'])

    wtbx_gdf = wtbx_gdf.dissolve(source_id_col, as_index=False, sort=False, aggfunc={
        'FID': partial(select_most_downstream_fid, GB=GB),
        'topological_order': 'min',
        'STRAHLER': 'max',
    })
    wtbx_gdf['geometry'] = wtbx_gdf['geometry'].apply(lambda geom: line_merge(geom))
    wtbx_gdf[source_id_col] = wtbx_gdf[source_id_col].astype(int)

    # Compute DSLINKNO for the conflated streams
    fid_to_dsfid = {}
    for fid in wtbx_gdf['FID']:
        downstream_fid = next(GB.successors(fid), None)
        if downstream_fid is not None:
            fid_to_dsfid[fid] = downstream_fid
        else:
            fid_to_dsfid[fid] = -1

    wtbx_gdf[source_ds_col] = wtbx_gdf['FID'].map(fid_to_dsfid)
    wtbx_gdf[source_ds_col] = wtbx_gdf[source_ds_col].map(lambda x: final_matches.get(x, -1) if x != -1 else -1)
    wtbx_gdf.loc[wtbx_gdf[source_id_col] == wtbx_gdf[source_ds_col], source_ds_col] = -1 # Remove self-loops (since we filtered out the borders)
    source_id_to_strm_order = source_gdf.set_index(source_id_col)[strm_order_col].to_dict()
    wtbx_gdf[strm_order_col] = wtbx_gdf[source_id_col].map(source_id_to_strm_order)

    # If there are any single streams (no upstream, no downstream), we should remove them
    single_streams = (wtbx_gdf[source_ds_col] == -1) & ~(wtbx_gdf[source_id_col].isin(source_gdf[source_ds_col]))
    wtbx_gdf = wtbx_gdf[~single_streams]

    # Map all other columns from the source_gdf to the wtbx_gdf based on the LINKNO
    source_columns = [col for col in source_gdf.columns if col not in [source_id_col, source_ds_col, strm_order_col, 'geometry', 'buffered']]
    mapped_source_columns = {
        col: wtbx_gdf[source_id_col].map(source_gdf.set_index(source_id_col)[col].to_dict())
        for col in source_columns
    }

    wtbx_gdf = pd.concat([wtbx_gdf, pd.DataFrame(mapped_source_columns, index=wtbx_gdf.index)], axis=1).copy()

    # If there is a wtbx stream that has a linkno, where that linkno is in the source gdf and is a multilinstring in the source, we will remove them

    if drop_multilinestrings:
        # Whether a reach is genuinely in disconnected pieces has to be decided after a
        # line_merge, not read off the stored geometry type. make_stream_geometry() does merge
        # before writing, but DEM_StrmShp is a GeoPackage unless streams_as_parquet is set, and
        # a GeoPackage layer promotes every LineString back to MultiLineString on the way in --
        # so the stored type says "MultiLineString" for every reach and carries no information.
        # Taken at face value this test drops the entire network: on the N14W89 domain it threw
        # away all 665 reaches and conflation returned nothing, when only 165 are really split.
        merged_source = source_gdf.geometry.line_merge()
        source_multilines = set(
            source_gdf.loc[merged_source.geom_type == 'MultiLineString', source_id_col]
        )
        wtbx_gdf = wtbx_gdf[~wtbx_gdf[source_id_col].isin(source_multilines)].copy()

    # Move linkno to the front
    cols = wtbx_gdf.columns.tolist()
    cols.insert(0, cols.pop(cols.index(source_id_col)))
    cols.insert(1, cols.pop(cols.index(source_ds_col)))
    wtbx_gdf = wtbx_gdf[cols]
    wtbx_gdf = wtbx_gdf[wtbx_gdf['geometry'].geom_type == 'LineString'].copy()
    wtbx_gdf = wtbx_gdf.set_crs(dem_proj) # Do this again, sometimes it is erased
    
    return wtbx_gdf

def remove_select_lake_streams(
    wtbx_gdf: gpd.GeoDataFrame,
    lakes_gdf: gpd.GeoDataFrame,
    wtbx_id_col: str = "FID",
    wtbx_ds_col: str = "DS_LINK_ID"
):
    intersecting_streams = wtbx_gdf.geometry.intersects(lakes_gdf.union_all())
    # We only want to remove streams that also have downstream and upstream segments outside the lake
    # Let's find those, and remove them from the intersecting_streams mask
    fids = set(wtbx_gdf[intersecting_streams][wtbx_id_col].tolist())
    if fids:
        GB: nx.DiGraph = nx.from_pandas_edgelist(wtbx_gdf, source=wtbx_id_col, target=wtbx_ds_col, create_using=nx.DiGraph)
        GB.remove_node(-1)
        for fid in fids:
            # Do any of descendants and ancestors of this fid exist outside the lake?
            descendants = nx.descendants(GB, fid)
            # See if any descendants are not in fids
            if not any(d not in fids for d in descendants):
                continue
            ancestors = nx.ancestors(GB, fid)
            if not any(a not in fids for a in ancestors):
                continue

            intersecting_streams[wtbx_gdf[wtbx_id_col] == fid] = False  # Keep this stream, since it has upstream and downstream segments outside the lake

    fids_to_remove = set(wtbx_gdf[intersecting_streams][wtbx_id_col].tolist())
    wtbx_gdf[wtbx_ds_col] = wtbx_gdf[wtbx_ds_col].apply(lambda x: x if x not in fids_to_remove else -1)
    wtbx_gdf = wtbx_gdf[~intersecting_streams]  # Remove streams that intersect lakes

    return wtbx_gdf

def _conflate_streams(
    source_gdf: gpd.GeoDataFrame,
    streams_vector: os.PathLike,
    lakes_gdf: gpd.GeoDataFrame | None,
    buffer_distance: float,
    dem_proj: str,
    dem_bbox: tuple[float, float, float, float],
    source_id_col: str = "LINKNO",
    source_ds_col: str = "DSLINKNO",
    wtbx_id_col: str = "FID",
    wtbx_ds_col: str = "DS_LINK_ID",
    strm_order_col: str = "strmOrder",
    drop_multilinestrings: bool = False
) -> gpd.GeoDataFrame:
    """
    Read files, build graphs, match the stream networks, and return the mapping state.
    """
    wtbx_vector = Vector(streams_vector)
    wtbx_gdf = wtbx_vector.to_geopandas().set_crs(dem_proj)
    if wtbx_gdf.empty:
        return wtbx_gdf

    wtbx_gdf[wtbx_ds_col] = wtbx_gdf[wtbx_ds_col].clip(lower=-1)  # Ensure no outlet is -1
    
    bounds = wtbx_vector.bbox

    # Filter source_gdf to only include streams that intersect the bounds of the wtbx_gdf, shrunk by a small distance
    source_gdf = source_gdf[source_gdf.intersects(box(*bounds))].copy()
    source_gdf = source_gdf.to_crs(dem_proj)

    LOG.info(f"Conflation input: {len(source_gdf)} source reaches, {len(wtbx_gdf)} Whitebox reaches.")
    if strm_order_col not in source_gdf.columns:
        raise ValueError(f"Source GeoDataFrame must have a '{strm_order_col}' column.")
    min_strm_order = source_gdf[strm_order_col].min()

    # Because we are clipped, let us set any dslinkno in the source that isn't in the source to -1, since it is outside the AOI
    valid_linknos = set(source_gdf[source_id_col]) | {-1}
    source_gdf[source_ds_col] = source_gdf[source_ds_col].apply(lambda x: x if x in valid_linknos else -1)

    wtbx_gdf = split_network_at_confluences(wtbx_gdf, source_gdf, buffer_distance, wtbx_id_col, wtbx_ds_col)

    # Ok, so the filled DEM removes any endorheic basins, but the source gdf has them
    # We need to detect this first and cut any wtbx ids that connect an outlet of an endorheic basin to a headwater in the source gdf
    # We can do this by first building our own graphs, finding all wtbx outlets, and the checking if any are not close to a source outlet
    wtbx_gdf = enforce_endorheic_basins(source_gdf, wtbx_gdf, source_id_col, source_ds_col, wtbx_id_col, wtbx_ds_col)

    if lakes_gdf is not None and not lakes_gdf.empty:
        wtbx_gdf = remove_select_lake_streams(wtbx_gdf, lakes_gdf, wtbx_id_col, wtbx_ds_col)
    LOG.info(f"After topology/lake pruning: {len(wtbx_gdf)} Whitebox reaches remain.")

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, message="Geometry is in a geographic CRS")
        wtbx_gdf['buffered'] = wtbx_gdf.buffer(buffer_distance, resolution=1, cap_style='square', join_style='mitre')
        source_gdf['buffered'] = source_gdf.simplify(tolerance=1e-5, preserve_topology=False).buffer(buffer_distance, resolution=1, cap_style='square', join_style='mitre')

    source_gdf['buffered'] = source_gdf["buffered"].make_valid()
    wtbx_gdf['buffered'] = wtbx_gdf["buffered"].make_valid()

    GA, GB = build_graphs(source_gdf, wtbx_gdf, min_strm_order, source_id_col, source_ds_col, wtbx_id_col, wtbx_ds_col)

    # Use a projected CRS for distance/length calculations
    wtbx_gdf[source_id_col] = np.nan

    A_reach_sig = compute_reach_signatures(GA)
    B_reach_sig = compute_reach_signatures(GB)

    # Get outlets of source graph
    source_headwaters = {n for n, sig in A_reach_sig.items() if sig.kind.is_source()}
    wtbx_headwaters = {n for n, sig in B_reach_sig.items() if sig.kind.is_source()}
    non_source_headwaters = set(GA.nodes()) - source_headwaters

    # Match headwaters
    best_source_scores = {}
    scores = {}
    match_to_source_headwaters(
        best_source_scores,
        scores,
        source_headwaters,
        wtbx_headwaters,
        A_reach_sig,
        B_reach_sig,
        wtbx_gdf,
        wtbx_id_col,
        min_strm_order,
        GB
    )

    match_others_to_source_headwaters(
        best_source_scores,
        scores,
        source_gdf,
        A_reach_sig,
        B_reach_sig,
        non_source_headwaters,
        source_id_col,
        GA
    )

    match_wtbx_headwaters(
        best_source_scores,
        scores,
        source_gdf,
        A_reach_sig,
        B_reach_sig,
        non_source_headwaters,
        wtbx_headwaters,
        source_id_col,
        GB,
        min_strm_order
    )

    headwater_fids, best_outlet_linkno_to_fid, headwater_fid_to_linkno = find_outlets_of_headwaters(
        best_source_scores,
        A_reach_sig,
        B_reach_sig,
        GA,
        GB
    )

    pairs = list((fid, linkno) for linkno, fid in best_outlet_linkno_to_fid.items()) + list(best_source_scores.values())
    final_matches = {}
    linkno_to_fid = defaultdict(set)
    for fid, linkno in pairs:
        final_matches[fid] = linkno
        linkno_to_fid[linkno].add(fid)

    fill_in_paths_between_headwaters_and_outlets(
        best_outlet_linkno_to_fid,
        headwater_fids,
        headwater_fid_to_linkno,
        A_reach_sig,
        B_reach_sig,
        GA,
        GB,
        final_matches,
        linkno_to_fid
    )

    fill_in_missing_branches(
        final_matches,
        linkno_to_fid,
        A_reach_sig,
        B_reach_sig,
        GB,
        source_gdf,
        source_id_col
    )

    wtbx_gdf = update_wtbx_gdf(
        wtbx_gdf,
        final_matches,
        source_id_col,
        source_ds_col,
        dem_bbox,
        buffer_distance,
        GB,
        source_gdf,
        strm_order_col,
        dem_proj,
        drop_multilinestrings
    )

    return wtbx_gdf

def select_most_downstream_fid(gdf: gpd.GeoSeries, GB: nx.DiGraph) -> gpd.GeoDataFrame:
    S = GB.subgraph(gdf.values)
    return [s for s in S.nodes if S.out_degree(s) == 0][0]


def _rasterize_streams(stream_raster: str, dem: str, streams_vector: str, attribute: str):
    dem_ds: gdal.Dataset = gdal.Open(dem)
    stream_ds: gdal.Dataset = gdal.GetDriverByName("GTiff").Create(stream_raster, dem_ds.RasterXSize, dem_ds.RasterYSize, 1, gdal.GDT_Int32, ['COMPRESS=DEFLATE', 'PREDICTOR=2'])
    stream_ds.SetGeoTransform(dem_ds.GetGeoTransform())
    stream_ds.SetProjection(dem_ds.GetProjection())
    options = gdal.RasterizeOptions(attribute=attribute)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        gdal.Rasterize(stream_ds, streams_vector, options=options)

# WhiteboxTools D8 pointer codes, mapped to the (row, col) offset of the cell each
# code drains into. Curve2Flood's FLDPLN spreader walks the stream info table with
# this same encoding, so the table has to be built against it.
_D8_OFFSETS = {
    64: (-1, -1), 128: (-1, 0), 1: (-1, 1),
    32: (0, -1),                2: (0, 1),
    16: (1, -1),    8: (1, 0),  4: (1, 1),
}

# How far the D8 path may run outside a reach's rasterized cells before we call the
# reach finished. Rasterizing a vector line and tracing D8 across a filled DEM pick
# slightly different cells, but over 95% of the gaps measured on real tiles are a
# single cell, so a short tolerance keeps a reach whole while still stopping a badly
# conflated reach from running away down the network.
_MAX_OFF_REACH_STEPS = 2

def _d8_offset_tables() -> tuple[np.ndarray, np.ndarray]:
    """Row/column offset per D8 code. An offset of (0, 0) means 'no outflow'."""
    row_offsets = np.zeros(256, np.int64)
    col_offsets = np.zeros(256, np.int64)
    for code, (d_row, d_col) in _D8_OFFSETS.items():
        row_offsets[code] = d_row
        col_offsets[code] = d_col
    return row_offsets, col_offsets

@njit(cache=True)
def _trace_reach(cells: np.ndarray, flowdir: np.ndarray, nrows: int, ncols: int,
                 row_offsets: np.ndarray, col_offsets: np.ndarray,
                 max_off_reach_steps: int) -> tuple[int, int, int]:
    """
    Follow the D8 flow direction through one reach's cells.

    ``cells`` is the sorted flat index of every pixel carrying this reach's link
    number. Returns ``(start_pixel, end_pixel, length)``: the pixel the longest D8
    path through those cells begins at, the last cell of the reach on that path, and
    the number of pixels the path visits, inclusive. Walking ``length`` pixels
    downstream from ``start_pixel`` therefore lands exactly on ``end_pixel``.
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
        flow_direction_file: str | Path) -> None:
    """
    Write the reach table that Curve2Flood's FLDPLN spreader reads.

    For each reach the spreader takes ``start_pixel`` and walks ``length`` pixels down
    the D8 flow direction raster, treating what it visits as that reach's stream
    pixels, then traces from ``end_pixel`` to exclude everything further downstream.
    Every row therefore has to describe a real D8 path. Taking ``length`` to be the
    number of rasterized cells carrying the link number, and the two endpoints from
    the ends of the stream vector, does not: rasterizing a line and tracing D8 across
    a filled DEM disagree about which cells belong to the reach, so the walk drifts
    off the reach and stops somewhere other than ``end_pixel``. The table is read off
    the flow direction raster instead, which is the same thing the spreader walks.
    """
    flow_direction_file = Path(flow_direction_file)
    if not flow_direction_file.exists():
        raise FileNotFoundError(
            f"Flow direction raster {flow_direction_file} does not exist. The FLDPLN stream info table is "
            "traced from it, so the hydrography has to be derived first (move_stream_network_to_thalweg)."
        )

    flowdir_ds: gdal.Dataset = gdal.Open(str(flow_direction_file))
    flowdir: np.ndarray = flowdir_ds.ReadAsArray()
    if flowdir.shape != streams_array.shape:
        raise ValueError(
            f"Flow direction raster {flow_direction_file} is {flowdir.shape} but the stream raster is "
            f"{streams_array.shape}. Both have to be on the DEM grid for the pixel indices to line up."
        )

    nrows, ncols = streams_array.shape
    linknos_flat = np.ascontiguousarray(streams_array).ravel()
    flowdir_flat = np.ascontiguousarray(flowdir).ravel().astype(np.int64, copy=False)
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
    if stream_info_file.suffix.lower() in {".parquet", ".pq"}:
        stream_info.to_parquet(stream_info_file, index=False)
    else:
        stream_info.to_csv(stream_info_file, index=False)


def make_channel_mask(channel_mask: np.ndarray, dem: str, water_mask: str, compression: str):
    dem_ds: gdal.Dataset = gdal.Open(dem)
    out_ds: gdal.Dataset = gdal.GetDriverByName('GTiff').Create(water_mask, dem_ds.RasterXSize, dem_ds.RasterYSize, 1, gdal.GDT_Byte, options=[f'COMPRESS={compression}'])
    out_ds.WriteArray(channel_mask)
    out_ds.SetGeoTransform(dem_ds.GetGeoTransform())
    out_ds.SetProjection(dem_ds.GetProjection())
    out_ds = None
