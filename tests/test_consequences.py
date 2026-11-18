import json
import subprocess
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from osgeo import gdal, osr
from shapely.geometry import Point

from nencarta.core.configs import NencartaConfig
from nencarta.core.floodmapper_output import FloodMapperBulkOutput, FloodMapperOutput
from nencarta.tasks import consequences
from nencarta.workspace import Workspace

NODATA = -9999.0


def make_workspace(workspace_tmp):
    dem = workspace_tmp / "dems" / "fabdem.tif"
    dem.parent.mkdir()
    dem.touch()
    config = {"name": "example", "dem": str(dem), "flowline": str(workspace_tmp / "streams.gpkg"),
              "output_dir": str(workspace_tmp / "out"), "floodmap_mode": "user",
              "user_flow_files": [str(workspace_tmp / "flows.csv")], "estimate_consequences": True}
    return Workspace(NencartaConfig(config), dem)


def write_depth_map(path, depth, epsg=4326, geotransform=(-93.0, 0.001, 0, 45.0, 0, -0.001)):
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), depth.shape[1], depth.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform(geotransform)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(NODATA)
    ds.GetRasterBand(1).WriteArray(depth)
    ds = None
    return path


def mapper_output(depth_map):
    output = FloodMapperBulkOutput([])
    output.outputs = [FloodMapperOutput(depth_map=depth_map)]
    return output


def test_go_consequences_is_given_the_depth_map_in_feet(workspace_tmp):
    workspace = make_workspace(workspace_tmp)
    depth = np.array([[NODATA, 0.5, 1.0], [2.0, 3.25, NODATA]], dtype=np.float32)
    depth_map = write_depth_map(workspace_tmp / "FloodMap" / "depth.tif", depth)

    [(command, results)] = consequences._get_consequences_tasks(mapper_output(depth_map), workspace)

    hazard = gdal.Open(str(depth_map.with_name("depth_WGS84_ft.tif")))
    feet = hazard.GetRasterBand(1).ReadAsArray()
    assert hazard.GetSpatialRef().GetAuthorityCode(None) == "4326"
    assert np.array_equal(feet == NODATA, depth == NODATA)
    assert np.allclose(feet[depth != NODATA], depth[depth != NODATA] / 0.3048)
    config = json.loads((workspace.Consequences_Folder / Path(command[-1]).name).read_text())
    assert config["hazard_provider_info"]["hazards"] == [
        {"hazard_parameter_type": "depth", "hazard_provider_file_path": "/data/hazard/depth_WGS84_ft.tif"}]
    assert results == workspace.Consequences_Folder / "depth_WGS84_ft_consequences.gpkg"


def test_a_projected_depth_map_is_given_in_wgs84(workspace_tmp):
    workspace = make_workspace(workspace_tmp)
    depth = np.full((20, 20), 2.0, dtype=np.float32)
    depth_map = write_depth_map(workspace_tmp / "FloodMap" / "depth.tif", depth, epsg=26915,
                                geotransform=(500000.0, 10.0, 0, 5000000.0, 0, -10.0))

    consequences._get_consequences_tasks(mapper_output(depth_map), workspace)

    hazard = gdal.Open(str(depth_map.with_name("depth_WGS84_ft.tif")))
    feet = hazard.GetRasterBand(1).ReadAsArray()
    assert hazard.GetSpatialRef().GetAuthorityCode(None) == "4326"
    assert np.allclose(feet[feet != NODATA], 2.0 / 0.3048)


def run_with(workspace_tmp, monkeypatch, caplog, returncode, stdout, structures_written):
    """Run run_consequences with a stand-in for Docker that writes this many structures and prints stdout."""
    workspace = make_workspace(workspace_tmp)
    depth_map = write_depth_map(workspace_tmp / "FloodMap" / "depth.tif", np.ones((3, 3), dtype=np.float32))

    def docker(command, **kwargs):
        results = workspace.Consequences_Folder / Path(command[-1]).name.replace(".json", ".gpkg")
        points = [Point(-93.0 + i * 0.001, 45.0) for i in range(structures_written)]
        gpd.GeoDataFrame({"depth": [1.0] * len(points)}, geometry=points, crs=4326).to_file(
            results, layer="results", driver="GPKG")
        return subprocess.CompletedProcess(command, returncode, stdout, "a stack trace")
    monkeypatch.setattr(consequences.shutil, "which", lambda name: name)
    monkeypatch.setattr(consequences.subprocess, "run", docker)
    with caplog.at_level("INFO", logger="nencarta"):
        consequences.run_consequences(mapper_output(depth_map), workspace)
    return [record.levelname for record in caplog.records if "Go-Consequences" in record.message
            or "consequences estimation" in record.message.lower()]


def test_results_written_before_go_consequences_crashes_on_closing_are_kept(workspace_tmp, monkeypatch, caplog):
    levels = run_with(workspace_tmp, monkeypatch, caplog, returncode=2, stdout="Closing, wrote 2 features\n",
                      structures_written=2)

    assert "WARNING" in levels and "ERROR" not in levels


def test_go_consequences_failing_is_an_error(workspace_tmp, monkeypatch, caplog):
    levels = run_with(workspace_tmp, monkeypatch, caplog, returncode=2, stdout="Closing, wrote 5 features\n",
                      structures_written=2)

    assert "ERROR" in levels


@pytest.mark.parametrize("failure", [
    'Get "https://nsi.sec.usace.army.mil/nsiapi/structures?bbox=": dial tcp: lookup nsi.sec.usace.army.mil: no such host',
    "NSI API request failed with status 503 Service Unavailable: ",
])
def test_structures_missing_from_the_nsi_are_an_error(workspace_tmp, monkeypatch, caplog, failure):
    stdout = f"https://nsi.sec.usace.army.mil/nsiapi/structures?bbox=\n{failure}\nClosing, wrote 0 features\n"

    levels = run_with(workspace_tmp, monkeypatch, caplog, returncode=0, stdout=stdout, structures_written=0)

    assert "ERROR" in levels


def test_without_docker_the_damage_is_skipped_with_an_error(workspace_tmp, monkeypatch, caplog):
    workspace = make_workspace(workspace_tmp)
    depth_map = write_depth_map(workspace_tmp / "FloodMap" / "depth.tif", np.ones((3, 3), dtype=np.float32))
    monkeypatch.setattr(consequences.shutil, "which", lambda name: None)

    with caplog.at_level("INFO", logger="nencarta"):
        consequences.run_consequences(mapper_output(depth_map), workspace)

    assert "Docker isn't installed" in caplog.text
    assert not depth_map.with_name("depth_WGS84_ft.tif").exists()
