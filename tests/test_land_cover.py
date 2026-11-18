"""The land cover raster on the DEM's grid (nencarta.tasks.make_land_cover)."""
import importlib

from osgeo import gdal, osr

from nencarta.core.configs import NencartaConfig
from nencarta.workspace import Workspace

# The module, not the function nencarta.tasks exports under its name
land_cover = importlib.import_module("nencarta.tasks.make_land_cover")


def test_the_tiles_are_warped_in_one_order_whatever_order_they_come_in(tmp_path, monkeypatch):
    # Where two ESA tiles meet, a cell the mode resampling takes from both gets the last one warped. The tiles come as
    # a set, whose order changes from one Python process to the next, so they are warped sorted.
    dem = tmp_path / "dem.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(dem), 4, 4, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((-99.5, 0.25, 0.0, 30.5, 0.0, -0.25))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds = None
    warped = []
    monkeypatch.setattr(land_cover, "_get_esa_tiles", lambda bbox: {"N30W102", "N27W099", "N30W099", "N27W102"})
    monkeypatch.setattr(land_cover.gdal, "Warp", lambda path, files, options=None: warped.append(list(files)))
    configs = NencartaConfig({"name": "tiles", "output_dir": str(tmp_path / "out"), "dem": str(dem),
                              "flowline": str(tmp_path / "streams.gpkg")})
    workspace = Workspace(configs, dem)

    land_cover.make_land_cover(workspace)

    assert warped == [[f"/vsis3/esa-worldcover/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
                       for tile in ("N27W099", "N27W102", "N30W099", "N30W102")]]
