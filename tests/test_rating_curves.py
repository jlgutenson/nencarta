"""Without a VDT database (make_vdt false), ARC's curve file stands in for it."""
from pathlib import Path

import pytest

from nencarta.core.configs import NencartaConfig
from nencarta.core.inspect_config import inspect_config
from nencarta.core.model_config import ModelConfig
from nencarta.tasks.configs import (
    define_arc_configs,
    define_configs_for_dem_cleaning,
    define_mapper_configs,
)
from nencarta.workspace import Workspace


def make_config(workspace_tmp, **overrides) -> dict:
    flow_file = workspace_tmp / "flows.csv"
    flow_file.write_text("COMID,flow\n1,10.0\n")
    config = {"name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
              "output_dir": str(workspace_tmp / "out"), "floodmap_mode": "user", "user_flow_files": [str(flow_file)]}
    config.update(overrides)
    return config


def test_without_a_vdt_the_maps_are_made_from_the_curve_file(workspace_tmp):
    configs = NencartaConfig(make_config(workspace_tmp, make_vdt=False))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")

    arc_config = define_arc_configs(workspace)
    mapper_config = define_mapper_configs(workspace, Path(configs.user_flow_files[0]))

    assert inspect_config(arc_config, "Print_VDT_Database") is None
    assert inspect_config(mapper_config, "Print_VDT_Database") is None
    assert Path(inspect_config(mapper_config, "Print_Curve_File")) == workspace.Curve_File_Bathy
    model_config = ModelConfig(arc_config, [mapper_config], [], workspace.mapper)
    assert not model_config.rating_curves_exist
    workspace.Curve_File_Bathy.touch()
    assert model_config.rating_curves_exist


def test_with_a_vdt_the_maps_are_made_from_it(workspace_tmp):
    configs = NencartaConfig(make_config(workspace_tmp))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")

    model_config = ModelConfig(define_arc_configs(workspace), [], [], workspace.mapper)
    workspace.Curve_File_Bathy.touch()
    assert not model_config.rating_curves_exist
    workspace.VDT_File_Bathy.touch()
    assert model_config.rating_curves_exist


@pytest.mark.parametrize("disable_bathymetry", [False, True])
def test_the_standard_step_links_arc_s_water_surfaces_along_the_network(workspace_tmp, disable_bathymetry):
    from arc.config import Configs

    configs = NencartaConfig(make_config(workspace_tmp, arc_args={"Standard_Step": True},
                                         disable_bathymetry=disable_bathymetry))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")
    arc_config = define_arc_configs(workspace)

    assert inspect_config(arc_config, "Standard_Step") in (True, "True")
    # ARC walks the cross sections down the reaches, so it has their ids even without bathymetry
    assert inspect_config(arc_config, "reach_id") == configs.stream_id_field
    assert inspect_config(arc_config, "downstream_reach_id") == configs.downstream_id_field
    assert Configs.from_file(arc_config).standard_step is True
    # And in the DEM cleaner's ARC run, whose rating curves map its first flood
    assert inspect_config(define_configs_for_dem_cleaning(workspace), "Standard_Step") in (True, "True")

    configs = NencartaConfig(make_config(workspace_tmp, disable_bathymetry=disable_bathymetry, overwrite=True))
    arc_config = define_arc_configs(Workspace(configs, workspace_tmp / "dem.tif"))
    assert inspect_config(arc_config, "Standard_Step") is None


@pytest.mark.parametrize("disable_bathymetry", [False, True])
def test_arc_args_reach_both_of_arc_s_runs(workspace_tmp, disable_bathymetry):
    from arc.config import Configs
    from curve2flood.config import Config as Curve2FloodConfig

    arc_args = {"Valley_Sections": True, "Valley_Dir_Widths": 12.5, "Drop_Edge_Sections": True, "Edge_Margin": 90}
    configs = NencartaConfig(make_config(workspace_tmp, arc_args=arc_args, disable_bathymetry=disable_bathymetry))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")
    arc_config = define_arc_configs(workspace)

    arc = Configs.from_file(arc_config)
    assert (arc.valley_sections, arc.valley_dir_widths, arc.drop_edge_sections, arc.edge_margin) == \
        (True, 12.5, True, 90.0)
    # nencarta's starting values, under the args
    assert arc.degree_manip == 6.1 and arc.findbanksbasedonlandcover is True
    # The file is ARC's alone: Curve2Flood burns the bathymetry in from a file of its own
    with pytest.raises(ValueError, match="Unrecognized config key"):
        Curve2FloodConfig.from_file(arc_config)
    # And in the DEM cleaner's ARC run, whose rating curves map its first flood
    cleaning = Configs.from_file(define_configs_for_dem_cleaning(workspace))
    assert (cleaning.valley_sections, cleaning.drop_edge_sections, cleaning.edge_margin) == (True, True, 90.0)

    configs = NencartaConfig(make_config(workspace_tmp, disable_bathymetry=disable_bathymetry, overwrite=True))
    arc_config = define_arc_configs(Workspace(configs, workspace_tmp / "dem.tif"))
    assert inspect_config(arc_config, "Valley_Sections") is None
    assert inspect_config(arc_config, "Drop_Edge_Sections") is None


@pytest.mark.parametrize("overrides", [
    {"make_vdt": False, "make_curvefile": False},
    {"make_vdt": False, "mapper": "Curve2Flood-FLDPLNpy"},
])
def test_configs_that_leave_the_flood_maps_without_rating_curves_raise(workspace_tmp, overrides):
    with pytest.raises(ValueError, match="make_vdt"):
        NencartaConfig(make_config(workspace_tmp, **overrides))


def write_stream_lines(path: Path, **columns) -> None:
    """A one-reach stream file at path with these attribute columns."""
    import geopandas as gpd
    from shapely.geometry import LineString

    lines = gpd.GeoDataFrame({"LINKNO": [1], "DSLINKNO": [-1], **{k: [v] for k, v in columns.items()}},
                             geometry=[LineString([(0.0, 0.0), (0.01, 0.01)])], crs=4326)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines.to_parquet(path) if path.suffix == ".parquet" else lines.to_file(path)


def arc_stream_lines(workspace: Workspace) -> Path:
    configs = workspace.configs
    return workspace.new_StrmShp_matched if configs.move_stream_network_to_thalweg else workspace.DEM_StrmShp


def test_arc_always_gets_the_width_power_law_in_km2(workspace_tmp):
    """Without the depth's power law in arc_args, ARC still gets the width's (nencarta's), so that it sizes the
    channels it can't see or find banks for; the stream lines have the area in km2."""
    from arc.config import Configs

    configs = NencartaConfig(make_config(workspace_tmp))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")
    write_stream_lines(arc_stream_lines(workspace), DSContArea=137e6, DSContArea_km2=137.0)
    arc_config = define_arc_configs(workspace)

    assert inspect_config(arc_config, "drainage_area_field") == configs.area_km2_field
    assert float(inspect_config(arc_config, "coefficient_width")) == pytest.approx(2.44)
    assert float(inspect_config(arc_config, "exponent_width")) == pytest.approx(0.34)
    assert inspect_config(arc_config, "coefficient_depth") is None
    arc = Configs.from_file(arc_config)
    assert arc.use_bathymetry_powerlaw_width and arc.coefficient_depth is None and arc.exponent_depth is None
    # And in the DEM cleaner's ARC run
    assert inspect_config(define_configs_for_dem_cleaning(workspace), "drainage_area_field") == configs.area_km2_field


def test_stream_lines_with_the_area_in_m2_get_the_same_widths(workspace_tmp):
    """A stream file made before nencarta always added the km2 field: ARC reads the m2 field, with the coefficient
    scaled to give the same width, and with the depth's power law the same depth."""
    configs = NencartaConfig(make_config(workspace_tmp, arc_args={"coefficient_depth": 0.27, "exponent_depth": 0.21}))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")
    write_stream_lines(arc_stream_lines(workspace), DSContArea=137e6)
    arc_config = define_arc_configs(workspace)

    assert inspect_config(arc_config, "drainage_area_field") == configs.area_m2_field
    width = float(inspect_config(arc_config, "coefficient_width")) * 137e6 ** float(
        inspect_config(arc_config, "exponent_width"))
    depth = float(inspect_config(arc_config, "coefficient_depth")) * 137e6 ** float(
        inspect_config(arc_config, "exponent_depth"))
    assert width == pytest.approx(2.44 * 137.0 ** 0.34)
    assert depth == pytest.approx(0.27 * 137.0 ** 0.21)


def test_stream_lines_without_a_drainage_area_get_no_power_law(workspace_tmp):
    configs = NencartaConfig(make_config(workspace_tmp))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")
    write_stream_lines(arc_stream_lines(workspace))

    assert inspect_config(define_arc_configs(workspace), "drainage_area_field") is None
