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
    assert arc_configs.makes_rating_curves
    assert Path(arc_configs.print_vdt_database) == workspace.VDT_File_Bathy


@pytest.mark.parametrize("overrides", MODES.values(), ids=MODES.keys())
def test_asking_for_them_changes_nothing_else_arc_does(workspace_tmp, overrides):
    without = Configs.from_file(define_arc_configs(make_workspace(
        workspace_tmp / "without", make_representative_cross_section_file=False, **overrides)))
    with_them = Configs.from_file(define_arc_configs(make_workspace(workspace_tmp / "with", **overrides)))

    for field in fields(Configs):
        if field.name in ("build_representative_cross_section", "representative_cross_section_file"):
            continue
        expected, actual = getattr(without, field.name), getattr(with_them, field.name)
        if isinstance(expected, str) and str(workspace_tmp) in expected:
            expected = expected.replace(str(workspace_tmp / "without"), str(workspace_tmp / "with"))
        assert actual == expected, field.name


@pytest.mark.parametrize("vdt_file_extension, suffix", [("txt", ".csv"), ("csv", ".csv"), ("parquet", ".parquet")])
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
