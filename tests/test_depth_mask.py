"""use_specified_depth_for_bathy_mask: ARC's water mask is the streams flooded to a set depth (make_depth_mask)."""
import numpy as np
import pytest
from osgeo import gdal, osr

from nencarta.core.configs import NencartaConfig
from nencarta.core.inspect_config import inspect_config
from nencarta.core.raster import Raster
from nencarta.tasks import make_bathy_water_mask
from nencarta.tasks.configs import define_arc_configs
from nencarta.tasks.make_bathy_water_mask import make_depth_mask
from nencarta.workspace import Workspace

CELL = 0.0001  # about 10 m
SIZE = 61
STREAM_COL = 30
DEPTH = 0.45


def write(path, array: np.ndarray, data_type: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, data_type)
    ds.SetGeoTransform((-100.0, CELL, 0, 40.0, 0, -CELL))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.WriteArray(array)
    ds = None


def make_workspace(root, **overrides) -> Workspace:
    """A flat-bottomed valley rising 0.1 m a cell either side of a stream down its middle column."""
    config = {"name": "valley", "dem": str(root / "dem.tif"), "flowline": str(root / "streams.gpkg"),
              "output_dir": str(root / "out"), "floodmap_mode": "return_period", "return_periods": [2],
              "use_specified_depth_for_bathy_mask": True, "specify_depths_for_bathy_mask": [DEPTH],
              "depth_mask_distance": 100, "use_yaml": True, "quiet": True}
    config.update(overrides)
    workspace = Workspace(NencartaConfig(config), root / "dem.tif")
    cols = np.arange(SIZE)
    write(workspace.assigned_dem, np.tile(100.0 + 0.1 * np.abs(cols - STREAM_COL), (SIZE, 1)).astype(np.float32),
          gdal.GDT_Float32)
    streams = np.zeros((SIZE, SIZE), np.int32)
    streams[:, STREAM_COL] = 1
    write(workspace.STRM_File_Clean, streams, gdal.GDT_Int32)
    return workspace


def test_the_streams_flood_to_the_set_depth(workspace_tmp):
    workspace = make_workspace(workspace_tmp)

    mask = Raster(make_depth_mask(workspace)).read_array() > 0

    # The stream's water surface is 100.45 m everywhere, so the cells under it are the 4 either side of the stream
    expected = np.zeros((SIZE, SIZE), bool)
    expected[:, STREAM_COL - 4:STREAM_COL + 5] = True
    np.testing.assert_array_equal(mask, expected)
    config = workspace.Depth_Mask_Config
    assert float(inspect_config(config, "Set_Depth")) == DEPTH
    assert float(inspect_config(config, "TopWidthPlausibleLimit")) == 100
    assert inspect_config(config, "DEM_File") == str(workspace.model_dem)
    assert inspect_config(config, "Stream_File") == str(workspace.STRM_File_Clean)


def test_an_existing_mask_is_kept(workspace_tmp, monkeypatch):
    workspace = make_workspace(workspace_tmp, overwrite=False)
    workspace.depth_mask.parent.mkdir(parents=True, exist_ok=True)
    workspace.depth_mask.touch()
    monkeypatch.setattr(make_bathy_water_mask, "Curve2Flood_MainFunction",
                        lambda *args, **kwargs: pytest.fail("the mask was made again"))

    assert make_depth_mask(workspace) == workspace.depth_mask


@pytest.mark.parametrize("set_depth", [True, False, None])
def test_arc_gets_the_mask_of_the_choice(workspace_tmp, set_depth):
    # None: the default, the water mask
    workspace = make_workspace(workspace_tmp, use_specified_depth_for_bathy_mask=set_depth)

    arc_config = define_arc_configs(workspace)

    mask = workspace.depth_mask if set_depth else workspace.bathy_water_mask
    assert inspect_config(arc_config, "BathyWaterMask") == str(mask)
    # ARC doesn't read the depth: its mask was flooded to it
    assert inspect_config(arc_config, "FloodSpreader_SpecifyDepth") is None
