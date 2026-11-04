"""What the flood maps' configs ask of the mapper."""
from pathlib import Path

import pytest

from nencarta.core.configs import NencartaConfig
from nencarta.core.inspect_config import inspect_config
from nencarta.tasks.configs import define_mapper_configs
from nencarta.workspace import Workspace


@pytest.mark.parametrize("args", [{}, {"solver": "exact", "max_cells_per_segment": 1000, "spill_decay": 0.005}],
                         ids=["defaults", "set"])
def test_the_fldpln_library_is_built_with_fldpln_library_args(workspace_tmp, monkeypatch, args):
    """nencarta passes fldpln_library_args on, and otherwise none of the library's settings: its own defaults hold,
    the depth-stepped solver 0.5 m a step, spilling once per step, without spill decay."""
    import inspect

    import curve2flood

    from nencarta.tasks.run_models import run_fldpln_library

    configs = NencartaConfig({
        "name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "mapper": "Curve2Flood-FLDPLNpy",
        "move_stream_network_to_thalweg": True, "fldpln_library_args": args,
    })
    workspace = Workspace(configs, workspace_tmp / "dem.tif")
    for path in (workspace.model_StrmShp, workspace.stream_info_file, workspace.VDT_File_Bathy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    defaults = {name: p.default for name, p in inspect.signature(curve2flood.build_fldpln_library).parameters.items()}
    calls = []
    monkeypatch.setattr(curve2flood, "build_fldpln_library", lambda **kwargs: calls.append(kwargs))

    run_fldpln_library(None, workspace)

    assert len(calls) == 1
    settings = {key: value for key, value in calls[0].items() if key not in
                ("dem", "filled_dem", "stream_info_file", "flow_direction_file", "library_file", "vdt_file", "pbar",
                 "bg_mask", "water_mask")}
    assert settings == args
    assert (defaults["solver"], defaults["dh"], defaults["iterative_spill"], defaults["spill_decay"]) == \
        ("iterative", 0.5, False, 0.0)


@pytest.mark.parametrize("has_mask", [False, True], ids=["no water mask", "water mask"])
def test_the_fldpln_library_is_built_with_the_bathymetry_water_mask(workspace_tmp, monkeypatch, has_mask):
    """The water mask, where there is one, widens each segment's TD(R) to the channel below it."""
    import curve2flood
    import numpy as np
    from osgeo import gdal, osr

    from nencarta.tasks.run_models import run_fldpln_library

    configs = NencartaConfig({
        "name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "mapper": "Curve2Flood-FLDPLNpy",
    })
    workspace = Workspace(configs, workspace_tmp / "dem.tif")
    for path in (workspace.model_StrmShp, workspace.stream_info_file, workspace.VDT_File_Bathy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    water = np.array([[0, 1, 1, 0], [0, 0, 1, 0], [0, 0, 1, 1]], dtype=np.uint8)
    if has_mask:
        workspace.bathy_water_mask.parent.mkdir(parents=True, exist_ok=True)
        ds = gdal.GetDriverByName("GTiff").Create(str(workspace.bathy_water_mask), 4, 3, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((-84.0, 0.001, 0.0, 34.0, 0.0, -0.001))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(water)
        ds = None
    calls = []
    monkeypatch.setattr(curve2flood, "build_fldpln_library", lambda **kwargs: calls.append(kwargs))

    run_fldpln_library(None, workspace)

    assert len(calls) == 1
    if has_mask:
        np.testing.assert_array_equal(calls[0]["water_mask"], water)
    else:
        assert calls[0]["water_mask"] is None


@pytest.mark.parametrize("flood_bathy_mask", [False, True])
def test_the_flood_maps_flood_the_bathymetry_water_mask_with_flood_bathy_mask(workspace_tmp, flood_bathy_mask):
    flow_file = workspace_tmp / "flows.csv"
    flow_file.write_text("COMID,flow\n1,10.0\n")
    configs = NencartaConfig({
        "name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "floodmap_mode": "user", "user_flow_files": [str(flow_file)],
        "curve2flood_args": {"Flood_Bathy_Mask": flood_bathy_mask or None},
    })
    workspace = Workspace(configs, workspace_tmp / "dem.tif")

    mapper_config = define_mapper_configs(workspace, flow_file)

    if flood_bathy_mask:
        assert inspect_config(mapper_config, "Flood_Bathy_Mask") in (True, "True")
        assert Path(inspect_config(mapper_config, "BathyWaterMask")) == workspace.bathy_water_mask
    else:
        assert inspect_config(mapper_config, "Flood_Bathy_Mask") is None
        assert inspect_config(mapper_config, "BathyWaterMask") is None


@pytest.mark.parametrize("args", [{}, {"FLDPLN_Min_Flood_Depth": 0.05, "TW_MultFact": 2.0}], ids=["defaults", "set"])
def test_the_flood_maps_get_curve2flood_args(workspace_tmp, args):
    """Over nencarta's starting values (TW_MultFact 1.5, TopWidthPlausibleLimit 6000), in a file Curve2Flood reads."""
    from curve2flood.config import Config

    flow_file = workspace_tmp / "flows.csv"
    flow_file.write_text("COMID,flow\n1,10.0\n")
    configs = NencartaConfig({
        "name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "floodmap_mode": "user", "user_flow_files": [str(flow_file)],
        "mapper": "Curve2Flood-FLDPLNpy", "curve2flood_args": args,
    })
    workspace = Workspace(configs, workspace_tmp / "dem.tif")

    config = Config.from_file(define_mapper_configs(workspace, flow_file))

    assert config.fldpln_min_flood_depth == args.get("FLDPLN_Min_Flood_Depth", 0.01)
    assert (config.tw_multfact, config.topwidthplausiblelimit) == (args.get("TW_MultFact", 1.5), 6000)


@pytest.mark.parametrize("velocity, gpkg", [(False, False), (True, True)], ids=["flood only", "velocity and polygons"])
def test_a_curve2flood_fldpln_config_holds_only_what_curve2flood_reads(workspace_tmp, velocity, gpkg):
    """The FLDPLN mapper reads neither the filled DEM nor the stream network; Curve2Flood reads the stream raster only
    with Set_Depth, Manning's n only for velocity, and the stream lines only to allow the GeoPackage, which it names
    after OutFLD."""
    flow_file = workspace_tmp / "flows.csv"
    flow_file.write_text("COMID,flow\n1,10.0\n")
    configs = NencartaConfig({
        "name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "floodmap_mode": "user", "user_flow_files": [str(flow_file)],
        "mapper": "Curve2Flood-FLDPLNpy", "make_velocity_maps": velocity,
        "curve2flood_args": {"Make_Output_GPKG": gpkg},
    })
    workspace = Workspace(configs, workspace_tmp / "dem.tif")

    config = inspect_config(define_mapper_configs(workspace, flow_file))

    for key in ("Filled_DEM_File", "reach_id", "downstream_reach_id", "Stream_File", "OutSHP"):
        assert key not in config
    for key in ("Flow_Direction_File", "Stream_Info_File", "FLDPLN_Library", "Print_VDT_Database"):
        assert key in config
    assert ("LU_Manning_n" in config) == velocity
    assert config.get("Make_Output_GPKG", True) in (gpkg, str(gpkg))


def test_a_floodspreader_config_keeps_the_stream_inputs(workspace_tmp):
    flow_file = workspace_tmp / "flows.csv"
    flow_file.write_text("COMID,flow\n1,10.0\n")
    configs = NencartaConfig({
        "name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "floodmap_mode": "user", "user_flow_files": [str(flow_file)],
        "mapper": "FloodSpreader", "make_velocity_maps": False,
    })
    workspace = Workspace(configs, workspace_tmp / "dem.tif")

    config = inspect_config(define_mapper_configs(workspace, flow_file))

    for key in ("Stream_File", "LU_Manning_n"):
        assert key in config
