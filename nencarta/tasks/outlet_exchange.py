"""
The outlet exchange (outlet_exchange): ARC starts a river that leaves a buffered DEM from normal depth, which is often
metres off, and the error backs up tens of kilometres, a seam where two tiles' maps meet. Run in two passes, each
DEM's rivers start instead from the water its neighbouring DEMs have where the rivers leave it, which the
neighbours' own outlets, far downstream, set.

pass1 (write_boundary_wse) runs ARC on only the reaches the neighbours start their outlets on, those crossing the
lines where the neighbours' buffers end inside this DEM, with every reach below them (the standard step marches up
from the outlets, so a reach's water depends on those below it) and the reaches flowing straight into those (bed
smoothing starts each reach's bed from the lowest of its inflows'). Their water near those lines goes to a file in
the folder the DEMs share.

pass2 (make_outlet_wse_file), once every DEM has done pass1, gathers the neighbours' water inside this DEM's buffer
into ARC's Outlet_WSE_File. ARC starts an outlet from the point nearest its last cross section, if one is within 3
cells, and the rest from normal depth.
"""
import json
import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from osgeo import osr
from shapely.geometry import LineString, MultiLineString, box

from nencarta.core.inspect_config import inspect_config
from nencarta.core.model_config import ModelConfig
from nencarta.core.raster import Raster
from nencarta.logger import LOG
from nencarta.workspace import Workspace

METADATA_KEY = b"nencarta_outlet_exchange"
BOUNDARY_COLUMNS = ["reach_id", "x", "y", "flow", "wse"]
EDGE_CELLS = 3  # how far past the lines, in cells, pass1 keeps the water: a neighbour's last cross section can be there


def own_bounds(workspace: Workspace) -> tuple[float, float, float, float]:
    """The DEM's own extent, without its buffer (minx, miny, maxx, maxy): the input DEM's, or else the cut DEM's
    shrunk by buffer_distance, as unbuffer_maps crops the maps to."""
    if workspace.original_dem:
        return Raster(workspace.original_dem).bbox
    
    minx, miny, maxx, maxy = Raster(workspace.assigned_dem).bbox
    distance = workspace.configs.buffer_distance
    return minx + distance, miny + distance, maxx - distance, maxy - distance


def buffer_lines(bounds: tuple[float, float, float, float], distance: float) -> MultiLineString:
    """The lines inside a DEM's own extent where its neighbours' buffers end, `distance` in from each edge, across
    the whole extent: a diagonal neighbour's buffer ends on them too."""
    minx, miny, maxx, maxy = bounds
    return MultiLineString([LineString([(minx + distance, miny), (minx + distance, maxy)]),
                            LineString([(maxx - distance, miny), (maxx - distance, maxy)]),
                            LineString([(minx, miny + distance), (maxx, miny + distance)]),
                            LineString([(minx, maxy - distance), (maxx, maxy - distance)])])


def boundary_reaches(network: gpd.GeoDataFrame, id_field: str, downstream_field: str,
                     lines: MultiLineString) -> tuple[set[int], set[int]]:
    """The reaches crossing the lines, and those with every reach below them on the network and the reaches flowing
    straight into any of those: what pass1 runs ARC on."""
    ids = network[id_field].astype(np.int64).to_numpy()
    crossing = set(ids[network.geometry.intersects(lines).to_numpy()].tolist())
    down = dict(zip(ids.tolist(), network[downstream_field].astype(np.int64).tolist()))
    ups: dict[int, list[int]] = {}
    for reach, below in down.items():
        ups.setdefault(below, []).append(reach)

    cut = set()
    for reach in crossing:
        while reach in down and reach not in cut:
            cut.add(reach)
            reach = down[reach]

    cut |= {up for reach in list(cut) for up in ups.get(reach, [])}
    return crossing, cut


def run_boundary_pass(model_config: ModelConfig, workspace: Workspace) -> ModelConfig:
    """
    outlet_exchange pass1: run ARC on the reaches the neighbouring DEMs start their outlets on (see the notes above),
    and write their water near the lines where the neighbours' buffers end to workspace.boundary_wse_file, with the
    DEM's own extent and coordinate system in its metadata. Skipped when that file exists, unless overwrite.
    """
    configs = workspace.configs
    if model_config.arc_config is None or not Path(model_config.arc_config).exists():
        LOG.info("No ARC config was made for this DEM; no water for its neighbours.")
        return model_config
    
    if workspace.boundary_wse_file.exists() and not configs.overwrite:
        LOG.info(f"{workspace.boundary_wse_file} already exists and we aren't making it again...")
        return model_config

    params = inspect_config(Path(model_config.arc_config))
    network_file = params.get("StrmShp_File")
    if not network_file or not Path(network_file).exists():
        LOG.info("This DEM has no stream network; no water for its neighbours.")
        return model_config

    dem = Raster(params["DEM_File"])
    bounds = own_bounds(workspace)
    lines = buffer_lines(bounds, configs.buffer_distance)
    network = gpd.read_parquet(network_file) if str(network_file).endswith(".parquet") else gpd.read_file(network_file)
    if network.crs is not None and not network.crs.equals(dem.projection):
        network = network.to_crs(dem.projection)

    crossing, cut = boundary_reaches(network, configs.stream_id_field, configs.downstream_id_field, lines)

    flows = pd.read_csv(params["Flow_File"])
    flows = flows[flows[params.get("Flow_File_ID", "COMID")].isin(cut)]
    LOG.info(f"outlet_exchange pass1: {len(crossing)} reaches cross where the neighbours' buffers end; ARC runs on "
             f"{len(flows)} reaches with those below them and their inflows")
    if crossing and not flows.empty:
        flows.to_csv(workspace.boundary_flow_file, index=False)
        _run_boundary_arc(params, workspace, model_config)
        water = _boundary_water(workspace.VDT_File_Boundary, dem, crossing, bounds, configs.buffer_distance)
    else:
        water = pd.DataFrame({column: pd.Series(dtype="int64" if column == "reach_id" else "float64")
                              for column in BOUNDARY_COLUMNS})

    metadata = {"bounds": list(bounds), "crs": dem.projection, "buffer_distance": configs.buffer_distance,
                "dem": workspace.dem_name}
    _write_boundary_file(water, metadata, workspace.boundary_wse_file)
    LOG.info(f"outlet_exchange pass1: {water[['reach_id', 'x', 'y']].drop_duplicates().shape[0]} points of "
             f"{water.reach_id.nunique()} reaches written to {workspace.boundary_wse_file}")
    return model_config


def _run_boundary_arc(params: dict, workspace: Workspace, model_config: ModelConfig) -> None:
    """ARC on the main run's config with pass1's flow file: only its rating curves, to their own file."""
    from nencarta.tasks.configs import _write_config
    from nencarta.tasks.run_models import _run_arc

    params = dict(params)
    for key in ("Print_Curve_File", "Print_AP_Database", "XS_Out_File", "Build_Representative_Cross_Section",
                "Representative_Cross_Section_File", "Outlet_WSE_File"):
        params.pop(key, None)
    params["Flow_File"] = workspace.boundary_flow_file
    params["Print_VDT_Database"] = workspace.VDT_File_Boundary
    params["Reach_Average_Curve_File"] = False
    if params.get("BATHY_Out_File"):
        # The bathymetry runs only when ARC writes it, and the rating curves need it
        params["BATHY_Out_File"] = workspace.ARC_BathyFile_Boundary

    _write_config(workspace.ARC_FileName_Boundary, params)
    try:
        _run_arc(workspace.ARC_FileName_Boundary, model_config)
    finally:
        workspace.ARC_BathyFile_Boundary.unlink(missing_ok=True)


def _boundary_water(vdt_file: Path, dem: Raster, reaches: set[int], bounds: tuple, distance: float) -> pd.DataFrame:
    """The water of these reaches' rated cells inside the DEM's own extent, no further in from its edges than the
    lines (and EDGE_CELLS cells past them), one row per cell and flow."""
    schema = pq.read_schema(vdt_file).names
    increments = sorted(int(name[2:]) for name in schema if name.startswith("q_") and name[2:].isdigit())
    vdt = pd.read_parquet(vdt_file, columns=["COMID", "Row", "Col"] + [f"{kind}_{i}" for i in increments
                                                                         for kind in ("q", "wse")])
    vdt = vdt[vdt.COMID.isin(reaches)]
    x0, width, _, y0, _, height = dem.geotransform
    x = x0 + (vdt.Col.to_numpy() + 0.5) * width
    y = y0 + (vdt.Row.to_numpy() + 0.5) * height
    minx, miny, maxx, maxy = bounds
    inward = np.minimum.reduce([x - minx, maxx - x, y - miny, maxy - y])
    keep = (inward >= 0.0) & (inward <= distance + EDGE_CELLS * max(abs(width), abs(height)))
    vdt, x, y = vdt[keep], x[keep], y[keep]
    q = vdt[[f"q_{i}" for i in increments]].to_numpy(np.float64)
    wse = vdt[[f"wse_{i}" for i in increments]].to_numpy(np.float64)
    water = pd.DataFrame({"reach_id": np.repeat(vdt.COMID.to_numpy(np.int64), len(increments)),
                          "x": np.repeat(x, len(increments)), "y": np.repeat(y, len(increments)),
                          "flow": q.ravel(), "wse": wse.ravel()})
    return water.dropna(subset=["flow", "wse"]).reset_index(drop=True)


def _write_boundary_file(water: pd.DataFrame, metadata: dict, path: Path) -> None:
    """Write pass1's water with its metadata, whole or not at all: the neighbours' pass2 may read it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(water[BOUNDARY_COLUMNS], preserve_index=False)
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), METADATA_KEY: json.dumps(metadata)})
    partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
    pq.write_table(table, partial)
    os.replace(partial, path)


def make_outlet_wse_file(workspace: Workspace) -> Path | None:
    """
    outlet_exchange pass2: gather the water the other DEMs' pass1 wrote (their *_boundary_wse.parquet files in the
    shared folder) inside this DEM's raster into workspace.outlet_wse_file, ARC's Outlet_WSE_File, and return it.
    None, and no file, when no neighbour's water falls inside: the outlets then start from normal depth.
    """
    dem = Raster(workspace.assigned_dem)
    minx, miny, maxx, maxy = dem.bbox
    here = box(minx, miny, maxx, maxy)
    reference = osr.SpatialReference(wkt=dem.projection)
    pieces, neighbours = [], []
    for path in sorted(workspace.outlet_exchange_folder.glob("*_boundary_wse.parquet")):
        if path.name == workspace.boundary_wse_file.name:
            continue

        metadata = (pq.read_schema(path).metadata or {}).get(METADATA_KEY)
        if metadata is None:
            continue

        metadata = json.loads(metadata)
        if not here.intersects(box(*metadata["bounds"])):
            continue

        if not reference.IsSame(osr.SpatialReference(wkt=metadata["crs"])):
            LOG.warning(f"outlet_exchange pass2: {path.name} is in another coordinate system; its water is left out.")
            continue
        
        water = pd.read_parquet(path, filters=[("x", ">", minx), ("x", "<", maxx), ("y", ">", miny), ("y", "<", maxy)])
        if not water.empty:
            pieces.append(water)
            neighbours.append(metadata.get("dem", path.name))

    if not pieces:
        workspace.outlet_wse_file.unlink(missing_ok=True)
        LOG.info("outlet_exchange pass2: no neighbouring DEM's water falls inside this DEM; its outlets start from "
                 "normal depth.")
        return None

    water = pd.concat(pieces, ignore_index=True)[BOUNDARY_COLUMNS]
    workspace.outlet_wse_file.parent.mkdir(parents=True, exist_ok=True)
    water.to_parquet(workspace.outlet_wse_file, index=False)
    LOG.info(f"outlet_exchange pass2: {water[['reach_id', 'x', 'y']].drop_duplicates().shape[0]} points of "
             f"{water.reach_id.nunique()} reaches from {', '.join(neighbours)}")
    return workspace.outlet_wse_file
