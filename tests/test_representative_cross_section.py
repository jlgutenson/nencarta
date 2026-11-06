"""
ARC builds representative cross sections in its bathymetry run, alongside the rating curves and from the same cross
sections. These tests check that NenCarta asks for them there, that asking changes nothing else ARC does, and that a
workspace whose VDT exists still gets them.
"""
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest
from arc.config import Configs
from osgeo import gdal, osr

from nencarta.core.configs import NencartaConfig
from nencarta.core.model_config import ModelConfig
from nencarta.tasks.configs import define_arc_configs
from nencarta.tasks.run_models import run_arc_bathymetry
from nencarta.workspace import Workspace

MODES = {
    "baseflow bathymetry": {},
    "power-law bathymetry": {"arc_args": {"coefficient_depth": 0.27, "exponent_depth": 0.21}},
    "no bathymetry": {"disable_bathymetry": True},
}


def write_raster(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((-84.3, 0.001, 0, 34.1, 0, -0.001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.WriteArray(array)
    ds = None


def make_workspace(workspace_tmp: Path, **overrides) -> Workspace:
    dem = workspace_tmp / "dems" / "fabdem.tif"
    write_raster(dem, np.zeros((6, 6), np.float32))
    config = {
        "name": "example",
        "dem": str(dem),
        "flowline": str(workspace_tmp / "streams.parquet"),
        "output_dir": str(workspace_tmp / "out"),
        "streamflow_source": "GEOGLOWS",
        "make_fist_inputs": False,
        "make_representative_cross_section_file": True,
        "stream_length_field": "length_m",
        "specified_bathyflow_field": "baseflow",
        "specified_highflow_field": "max",
        "use_yaml": True,
    }
    config.update(overrides)
    return Workspace(NencartaConfig(config), dem)


@pytest.mark.parametrize("overrides", MODES.values(), ids=MODES.keys())
def test_the_bathymetry_run_builds_them_beside_the_rating_curves(workspace_tmp, overrides):
    workspace = make_workspace(workspace_tmp, **overrides)

    arc_configs = Configs.from_file(define_arc_configs(workspace))

    assert arc_configs.build_representative_cross_section
    assert Path(arc_configs.representative_cross_section_file) == workspace.Representative_Cross_Section_File
    assert workspace.XS_Folder == workspace.output_dir / "XS"
    assert workspace.Representative_Cross_Section_File.parent == workspace.XS_Folder
    assert workspace.representative_cross_section_coefficients_file.parent == workspace.XS_Folder
    assert workspace.XS_Folder.is_dir()
    assert arc_configs.makes_rating_curves
    assert Path(arc_configs.print_vdt_database) == workspace.VDT_File_Bathy


@pytest.mark.parametrize("overrides", MODES.values(), ids=MODES.keys())
def test_asking_for_them_changes_nothing_else_arc_does(workspace_tmp, overrides):
    without = Configs.from_file(define_arc_configs(make_workspace(
        workspace_tmp / "without", make_representative_cross_section_file=False, **overrides)))
    with_them = Configs.from_file(define_arc_configs(make_workspace(workspace_tmp / "with", **overrides)))

    for field in fields(Configs):
        if field.name in ("build_representative_cross_section", "representative_cross_section_file", "stream_length_m"):
            continue
        expected, actual = getattr(without, field.name), getattr(with_them, field.name)
        if isinstance(expected, str) and str(workspace_tmp) in expected:
            expected = expected.replace(str(workspace_tmp / "without"), str(workspace_tmp / "with"))
        assert actual == expected, field.name


@pytest.mark.parametrize("vdt_file_extension, suffix", [("txt", ".csv"), ("csv", ".csv"), ("parquet", ".csv")])
def test_file_is_parquet_with_a_parquet_vdt(workspace_tmp, vdt_file_extension, suffix):
    workspace = make_workspace(workspace_tmp, vdt_file_extension=vdt_file_extension)

    assert workspace.Representative_Cross_Section_File.suffix == suffix


def test_text_config(workspace_tmp):
    workspace = make_workspace(workspace_tmp, use_yaml=False)

    config = define_arc_configs(workspace)

    assert config.suffix == ".txt"
    assert Configs.from_file(config).build_representative_cross_section


@pytest.mark.parametrize("representative_exists, overwrite, runs", [
    (False, False, True),  # the VDT is there, but not the representative cross sections
    (True, False, False),
    (True, True, True),
])
def test_arc_reruns_for_missing_representative_cross_sections(workspace_tmp, monkeypatch, representative_exists,
                                                              overwrite, runs):
    workspace = make_workspace(workspace_tmp, overwrite=overwrite)
    workspace.DEM_StrmShp.parent.mkdir(parents=True, exist_ok=True)
    workspace.DEM_StrmShp.touch()
    config = define_arc_configs(workspace)
    workspace.VDT_File_Bathy.touch()
    if representative_exists:
        workspace.Representative_Cross_Section_File.touch()
        workspace.representative_cross_section_coefficients_file.touch()
    calls = []
    monkeypatch.setattr("nencarta.tasks.run_models._run_arc", lambda config, model_config: calls.append(config))

    run_arc_bathymetry(ModelConfig(config, [], [], workspace.mapper), workspace)

    assert calls == ([config] if runs else [])


def test_without_bathymetry_the_config_is_rewritten_for_missing_representative_cross_sections(workspace_tmp):
    workspace = make_workspace(workspace_tmp, disable_bathymetry=True, make_representative_cross_section_file=False)
    config = define_arc_configs(workspace)
    for output in (workspace.VDT_File_Bathy, workspace.Curve_File_Bathy, workspace.AP_File):
        output.touch()
    asked = make_workspace(workspace_tmp, disable_bathymetry=True)

    assert define_arc_configs(asked) == config
    assert Configs.from_file(config).build_representative_cross_section


def test_export_checkbox_does_not_select_representative_only_mode(workspace_tmp):
    """The additional export and mapper suppression remain distinct settings."""
    workspace = make_workspace(workspace_tmp)
    assert not workspace.configs.representative_cross_section_creation
    assert Configs.from_file(define_arc_configs(workspace)).makes_rating_curves


def test_representative_only_config_omits_rating_outputs_and_uses_lengths(workspace_tmp):
    workspace = make_workspace(workspace_tmp, representative_cross_section_creation=True,
                               make_representative_cross_section_file=False, disable_bathymetry=True,
                               clean_dem=True, use_specified_depth_for_bathy_mask=True)
    config = Configs.from_file(define_arc_configs(workspace))
    assert config.build_representative_cross_section and not config.makes_rating_curves
    assert config.stream_length_m == 'length_m'
    assert config.print_vdt_database is config.print_curve_file is config.print_ap_database is None
    assert not workspace.configs.clean_dem and not workspace.configs.use_specified_depth_for_bathy_mask


def test_all_mapper_entry_points_are_skipped_in_representative_only_mode(workspace_tmp, monkeypatch):
    from nencarta.tasks import run_models
    from nencarta.tasks.clean_dem import make_clean_dem
    from nencarta.tasks.make_bathy_water_mask import flood_streams_to_depth
    from nencarta.tasks.outlet_exchange import run_boundary_pass
    from nencarta.core.enumerations import Mapper
    workspace = make_workspace(workspace_tmp, representative_cross_section_creation=True)
    def forbidden(*args, **kwargs):
        pytest.fail('A mapper was activated for representative-only processing')
    monkeypatch.setattr(run_models, '_run_mapper', forbidden)
    monkeypatch.setattr('nencarta.tasks.make_bathy_water_mask.Curve2Flood_MainFunction', forbidden)
    for mapper in Mapper:
        workspace.configs.mapper = workspace.mapper = mapper
        model = ModelConfig(workspace.ARC_FileName_Bathy, [], [], mapper)
        assert run_models.run_mapper_bathymetry(model, workspace) is model
        assert run_models.run_fldpln_library(model, workspace) is model
        assert run_boundary_pass(model, workspace) is model
        run_models.run_mapper_floodmaps(model, workspace)
    make_clean_dem(workspace)
    assert flood_streams_to_depth(workspace, workspace.model_dem, 1., workspace.depth_mask,
                                  workspace.Depth_Mask_Config) is None


def test_representative_preparation_does_not_prepare_mapper_or_scenario_flows(workspace_tmp, monkeypatch):
    import importlib
    prepare = importlib.import_module('nencarta.tasks.prepare_inputs')
    workspace = make_workspace(workspace_tmp, representative_cross_section_creation=True,
                               disable_bathymetry=True, clean_dem=True)
    def forbidden(*args, **kwargs):
        pytest.fail('Mapper/scenario flow preparation was invoked for representative-only processing')
    for name in ('make_reanalysis_file', 'make_flood_flow_file_from_base_max_file', 'make_clean_dem',
                 'make_depth_mask', 'make_flow_file_from_forecast', 'define_mapper_configs', 'get_fist_inputs'):
        monkeypatch.setattr(prepare, name, forbidden)
    for name in ('assign_and_validate_dem', 'Create_BaseLine_Manning_n_File_ESA', 'make_land_cover',
                 'make_stream_geometry', 'make_stream_raster', 'make_water_mask', 'burn_streams_and_move_streams'):
        monkeypatch.setattr(prepare, name, lambda *args: None)
    model = prepare.prepare_inputs_for_dem(workspace)
    assert model.mapper_configs == model.fist_inputs == []
    assert model.bathy_mapper_config is None


def test_real_local_arc_export_runs_without_a_mapper(workspace_tmp, monkeypatch):
    """Exercise the sampler and representative export using a real generated config."""
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString
    from nencarta.tasks import run_models
    workspace = make_workspace(workspace_tmp, representative_cross_section_creation=True,
                               make_representative_cross_section_file=False, disable_bathymetry=True,
                               arc_args={'Degree_Manip': 0, 'Low_Spot_Range': 0,
                                         'Stream_Slope_Method': 'local_average'})
    rr, cc = np.indices((41, 31))
    write_raster(workspace.model_dem, 100 - cc * 0.2 + np.maximum(np.abs(rr - 20) - 3, 0) * 0.2)
    stream = np.zeros((41, 31), np.float32)
    stream[20, 7:24] = 1
    write_raster(workspace.STRM_File_Clean, stream)
    write_raster(workspace.LAND_File, np.ones_like(stream))
    workspace.mannings_n_text_file.parent.mkdir(parents=True, exist_ok=True)
    workspace.mannings_n_text_file.write_text('LC_ID\tDescription\tManning_n\n1\tLand\t0.07\n80\tWater\t0.03\n')
    workspace.DEM_StrmShp.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({'LINKNO': [1], 'DSLINKNO': [-1], 'length_m': [2400.]},
                     geometry=[LineString([(-84.2925, 34.0795), (-84.2765, 34.0795)])],
                     crs='EPSG:4326').to_file(workspace.DEM_StrmShp, driver='GPKG')
    model = ModelConfig(define_arc_configs(workspace), [], [], workspace.mapper)
    def forbidden(*args, **kwargs):
        pytest.fail('A mapper was activated during the real representative-only run')
    monkeypatch.setattr(run_models, '_run_mapper', forbidden)
    run_arc_bathymetry(model, workspace)
    run_models.run_mapper_bathymetry(model, workspace)
    run_models.run_mapper_floodmaps(model, workspace)
    stages = pd.read_csv(workspace.representative_cross_section_file)
    coefficients = pd.read_csv(workspace.representative_cross_section_coefficients_file)
    assert not stages.empty and set(stages.COMID) == {1}
    assert np.isfinite(coefficients.Celerity_K_Power_Law_A).all()
    assert not workspace.VDT_File_Bathy.exists()
