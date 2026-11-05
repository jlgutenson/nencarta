"""
outlet_exchange: pass1 runs ARC on only the reaches the neighbouring DEMs start their outlets on and writes their water
for them; pass2 gathers the neighbours' water inside the DEM's buffer into ARC's Outlet_WSE_File (see
nencarta/tasks/outlet_exchange.py). ARC is stood in for by a function that writes known water at known cells.
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from osgeo import gdal, osr
from shapely.geometry import LineString

from nencarta.core.configs import NencartaConfig
from nencarta.core.enumerations import Mapper
from nencarta.core.inspect_config import inspect_config
from nencarta.core.model_config import ModelConfig
from nencarta.main import run_pipeline
from nencarta.pipeline import build_pipeline
from nencarta.tasks import run_models
from nencarta.tasks.configs import _write_config, define_arc_configs
from nencarta.tasks.outlet_exchange import (
    METADATA_KEY,
    boundary_reaches,
    buffer_lines,
    make_outlet_wse_file,
    run_boundary_pass,
)
from nencarta.workspace import Workspace

CELL = 0.001
BUFFER = 0.02
# Two tiles 0.1 degrees across, side by side: west's own extent, then east's
WEST = (-91.1, 34.0, -91.0, 34.1)
EAST = (-91.0, 34.0, -90.9, 34.1)
FLOWS = np.array([10.0, 20.0, 30.0])


def write_raster(path: Path, bounds: tuple, epsg: int = 4326) -> None:
    minx, miny, maxx, maxy = bounds
    cols, rows = round((maxx - minx) / CELL), round((maxy - miny) / CELL)
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((minx, CELL, 0, maxy, 0, -CELL))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    ds.WriteArray(np.zeros((rows, cols), np.float32))
    ds = None


def buffered(bounds: tuple) -> tuple:
    minx, miny, maxx, maxy = bounds
    return minx - BUFFER, miny - BUFFER, maxx + BUFFER, maxy + BUFFER


def make_workspace(root: Path, name: str, bounds: tuple, **overrides) -> Workspace:
    """A buffered tile whose cut DEM is in place, as prepare_inputs_for_dem leaves it."""
    dem = root / "dems" / f"{name}.tif"
    write_raster(dem, bounds)
    config = {"name": name, "dem": str(dem), "source_dems": [str(dem)], "flowline": str(root / "streams.parquet"),
              "output_dir": str(root / "out"), "floodmap_mode": "return_period", "return_periods": [2],
              "buffer": True, "buffer_distance": BUFFER, "arc_args": {"Standard_Step": True}, "outlet_exchange": "pass1",
              "use_yaml": True, "quiet": True}
    config.update(overrides)
    workspace = Workspace(NencartaConfig(config), dem)
    write_raster(workspace.assigned_dem, buffered(bounds))
    return workspace


def east_network() -> gpd.GeoDataFrame:
    """
    The east tile's reaches: 1 comes in across the line where the west tile's buffer ends (x = -90.98) and runs to 2,
    which 4 joins (and 5 joins 4); 2 runs to 3, which leaves across the east line; 7 comes down across the north line
    to 2; 6 stays inside.
    """
    lines = {
        1: [(-91.0, 34.05), (-90.97, 34.05)],
        2: [(-90.97, 34.05), (-90.95, 34.05)],
        3: [(-90.95, 34.05), (-90.9, 34.05)],
        4: [(-90.96, 34.03), (-90.96, 34.05)],
        5: [(-90.96, 34.025), (-90.96, 34.03)],
        6: [(-90.95, 34.03), (-90.94, 34.03)],
        7: [(-90.95, 34.1), (-90.95, 34.05)],
    }
    down = {1: 2, 2: 3, 3: -1, 4: 2, 5: 4, 6: -1, 7: 2}
    return gpd.GeoDataFrame({"LINKNO": list(lines), "DSLINKNO": [down[r] for r in lines]},
                            geometry=[LineString(c) for c in lines.values()], crs=4326)


def test_the_reaches_crossing_where_the_buffers_end_with_those_below_and_their_inflows():
    crossing, cut = boundary_reaches(east_network(), "LINKNO", "DSLINKNO", buffer_lines(EAST, BUFFER))

    assert crossing == {1, 3, 7}
    # 2 and 3 are below them, and 4 flows straight into 2; 5 is two reaches up, and 6 is elsewhere
    assert cut == {1, 2, 3, 4, 7}


def write_arc_config(workspace: Workspace, network: gpd.GeoDataFrame) -> ModelConfig:
    """The main ARC config, as define_arc_configs writes it, over a flow file of every reach."""
    workspace.strm_folder.mkdir(parents=True, exist_ok=True)
    workspace.FLOW_Folder.mkdir(parents=True, exist_ok=True)
    network.to_parquet(workspace.new_StrmShp_matched)
    pd.DataFrame({"COMID": network.LINKNO, "rp2": 5.0, "rp100": 50.0}).to_csv(workspace.DEM_Reanalsyis_FlowFile,
                                                                             index=False)
    params = {"DEM_File": workspace.assigned_dem, "Stream_File": workspace.new_stream_raster,
              "Flow_File": workspace.DEM_Reanalsyis_FlowFile, "Flow_File_ID": "COMID", "Flow_File_BF": "rp2",
              "Flow_File_QMax": "rp100", "Print_VDT_Database": workspace.VDT_File_Bathy,
              "Print_Curve_File": workspace.Curve_File_Bathy, "StrmShp_File": workspace.new_StrmShp_matched,
              "Standard_Step": True, "BATHY_Out_File": workspace.ARC_BathyFile}
    _write_config(workspace.ARC_FileName_Bathy, params)
    return ModelConfig(workspace.ARC_FileName_Bathy, [], [], Mapper.CURVE2FLOOD_KERNEL_WEIGHTED, True)


def cell_at(x: float, y: float) -> tuple[int, int]:
    """The (row, col) of a point on the east tile's buffered raster."""
    minx, _, _, maxy = buffered(EAST)
    return int((maxy - y) / CELL), int((x - minx) / CELL)


@pytest.fixture
def stand_in_arc(monkeypatch):
    """ARC, as pass1 runs it: rates each reach of its flow file at four cells along 34.05N, whose centres are
    0.0105 degrees in from the east tile's west edge (inside the west tile's buffer), 0.0195 (the buffer's last
    cell), 0.0225 (2.5 cells past the line where the buffer ends) and 0.0505, at water 100 + the reach + the
    cell's column / 1000 + the flow's number."""
    seen = {}

    def run(config, model_config):
        params = inspect_config(Path(config))
        seen.update(params)
        Path(params["BATHY_Out_File"]).parent.mkdir(parents=True, exist_ok=True)
        Path(params["BATHY_Out_File"]).write_bytes(b"bathymetry")
        reaches = pd.read_csv(params["Flow_File"]).COMID.tolist()
        rows = []
        for reach in reaches:
            for x in (-90.99, -90.981, -90.978, -90.95):
                row, col = cell_at(x + CELL / 2, 34.05 - CELL / 2)
                rows.append({"COMID": reach, "Row": row, "Col": col,
                             **{f"q_{i}": q for i, q in enumerate(FLOWS, 1)},
                             **{f"wse_{i}": 100.0 + reach + col / 1000 + i for i in range(1, FLOWS.size + 1)}})
        Path(params["Print_VDT_Database"]).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(params["Print_VDT_Database"], index=False)

    monkeypatch.setattr(run_models, "_run_arc", run)
    return seen


def test_pass1_writes_the_water_of_the_crossing_reaches_near_the_lines(workspace_tmp, stand_in_arc):
    workspace = make_workspace(workspace_tmp, "east", EAST)
    model_config = write_arc_config(workspace, east_network())

    run_boundary_pass(model_config, workspace)

    # ARC ran on the cut reaches alone, to their own rating curves, and not to the main run's curve file
    assert Path(stand_in_arc["Flow_File"]) == workspace.boundary_flow_file
    assert set(pd.read_csv(workspace.boundary_flow_file).COMID) == {1, 2, 3, 4, 7}
    assert Path(stand_in_arc["Print_VDT_Database"]) == workspace.VDT_File_Boundary
    assert "Print_Curve_File" not in stand_in_arc
    assert not workspace.ARC_BathyFile_Boundary.exists()  # its bathymetry raster is only a means
    assert not workspace.VDT_File_Bathy.exists()

    water = pd.read_parquet(workspace.boundary_wse_file)
    metadata = json.loads(pq.read_schema(workspace.boundary_wse_file).metadata[METADATA_KEY])
    assert metadata["bounds"] == pytest.approx(EAST)
    assert osr.SpatialReference(wkt=metadata["crs"]).GetAuthorityCode(None) == "4326"
    # The crossing reaches only, at the cells up to the line and EDGE_CELLS past it, not 0.05 degrees in
    assert set(water.reach_id) == {1, 3, 7}
    assert sorted(water.x.round(4).unique()) == pytest.approx([-90.9895, -90.9805, -90.9775])
    assert len(water) == 3 * 3 * FLOWS.size
    one = water[(water.reach_id == 1) & np.isclose(water.x, -90.9805)].sort_values("flow")
    np.testing.assert_allclose(one.flow, FLOWS)
    col = cell_at(-90.9805, 34.0495)[1]
    np.testing.assert_allclose(one.wse, [101.0 + col / 1000 + i for i in range(1, 4)])


def test_pass1_is_skipped_when_its_file_exists_unless_overwrite(workspace_tmp, stand_in_arc):
    workspace = make_workspace(workspace_tmp, "east", EAST)
    model_config = write_arc_config(workspace, east_network())
    workspace.outlet_exchange_folder.mkdir(parents=True)
    workspace.boundary_wse_file.write_bytes(b"done")

    run_boundary_pass(model_config, workspace)

    assert not stand_in_arc
    assert workspace.boundary_wse_file.read_bytes() == b"done"


def test_pass2_gathers_the_neighbours_water_inside_the_dem(workspace_tmp, stand_in_arc):
    east = make_workspace(workspace_tmp, "east", EAST)
    run_boundary_pass(write_arc_config(east, east_network()), east)
    # A tile far away, and the east tile's water again but said to be in another coordinate system: both left out
    far = make_workspace(workspace_tmp, "far", (-80.1, 30.0, -80.0, 30.1))
    run_boundary_pass(write_arc_config(far, east_network()), far)
    utm = pa.Table.from_pandas(pd.read_parquet(east.boundary_wse_file), preserve_index=False)
    utm = utm.replace_schema_metadata({METADATA_KEY: json.dumps({"bounds": list(EAST), "crs": _wkt(32615),
                                                                 "dem": "utm"})})
    pq.write_table(utm, east.outlet_exchange_folder / "utm_boundary_wse.parquet")

    west = make_workspace(workspace_tmp, "west", WEST, outlet_exchange="pass2")
    outlet_file = make_outlet_wse_file(west)

    assert outlet_file == west.outlet_wse_file
    given = pd.read_parquet(outlet_file)
    # The east tile's points inside the west tile's buffer (west of -90.98); the one past the line is outside it
    assert set(given.reach_id) == {1, 3, 7}
    assert sorted(given.x.round(4).unique()) == pytest.approx([-90.9895, -90.9805])
    assert list(given.columns) == ["reach_id", "x", "y", "flow", "wse"]

    # ARC gets the file
    from arc.config import Configs
    arc_config = define_arc_configs(west)
    assert Path(inspect_config(arc_config, "Outlet_WSE_File")) == outlet_file
    assert Path(Configs.from_file(arc_config).outlet_wse_file) == outlet_file


def _wkt(epsg: int) -> str:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    return srs.ExportToWkt()


def test_without_a_neighbour_s_water_pass2_leaves_the_outlets_at_normal_depth(workspace_tmp):
    west = make_workspace(workspace_tmp, "west", WEST, outlet_exchange="pass2")
    west.outlet_wse_file.parent.mkdir(parents=True, exist_ok=True)
    west.outlet_wse_file.write_bytes(b"stale")

    assert make_outlet_wse_file(west) is None
    assert not west.outlet_wse_file.exists()
    assert inspect_config(define_arc_configs(west), "Outlet_WSE_File") is None


def test_only_pass2_gives_arc_the_outlet_file(workspace_tmp):
    for mode in ("off", "pass1"):
        workspace = make_workspace(workspace_tmp / mode, "west", WEST, outlet_exchange=mode)
        workspace.outlet_wse_file.parent.mkdir(parents=True, exist_ok=True)
        workspace.outlet_wse_file.write_bytes(b"left over")
        assert inspect_config(define_arc_configs(workspace), "Outlet_WSE_File") is None


def test_pass1_only_prepares_the_inputs_and_writes_the_water():
    assert [f.output_name for f in build_pipeline(False, "pass1").functions] == ["model_configs", "boundary_wse"]
    assert "mapper_outputs" in [f.output_name for f in build_pipeline(False, "pass2").functions]


def test_watersheds_run_together_share_the_pass(workspace_tmp):
    workspaces = [make_workspace(workspace_tmp, "west", WEST),
                  make_workspace(workspace_tmp, "east", EAST, outlet_exchange="pass2")]
    with pytest.raises(ValueError, match="share outlet_exchange"):
        run_pipeline(workspaces)


@pytest.mark.parametrize("overrides, message", [
    ({"outlet_exchange": "pass3"}, "must be one of"),
    ({"buffer": False}, "needs 'buffer'"),
    ({"arc_args": {"Standard_Step": False}}, "needs 'buffer'"),
])
def test_the_passes_need_a_buffer_and_the_standard_step(workspace_tmp, overrides, message):
    dem = workspace_tmp / "dem.tif"
    config = {"name": "x", "dem": str(dem), "source_dems": [str(dem)], "flowline": str(workspace_tmp / "s.gpkg"),
              "output_dir": str(workspace_tmp),
              "buffer": True, "arc_args": {"Standard_Step": True}, "outlet_exchange": "pass1", **overrides}
    with pytest.raises(ValueError, match=message):
        NencartaConfig(config)
