import numpy as np
from osgeo import gdal, osr

from nencarta.core.configs import NencartaConfig
from nencarta.core.floodmapper_output import FloodMapperBulkOutput
from nencarta.tasks.unbuffer import unbuffer_maps
from nencarta.workspace import Workspace

CELL = 0.01


def write_raster(path, array, x0, y0):
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, CELL, 0, y0, 0, -CELL))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.WriteArray(array)
    ds = None


def test_a_buffered_map_is_cropped_to_the_dem_north_up(workspace_tmp):
    dem = workspace_tmp / "dems" / "dem.tif"
    write_raster(dem, np.zeros((10, 10), np.float32), -105.0, 41.0)
    workspace = Workspace(NencartaConfig({
        "name": "example", "dem": str(dem), "flowline": str(workspace_tmp / "streams.gpkg"),
        "output_dir": str(workspace_tmp / "out"), "buffer": True, "buffer_distance": 2 * CELL,
        "source_dems": [str(dem)],
    }), dem)
    # The map of the DEM grown by two cells on every side
    buffered = np.arange(14 * 14, dtype=np.float32).reshape(14, 14)
    flood_map = workspace.flood_folder / "GEOGLOWS_dem_buffered_ARC_Flood_rp2.tif"
    write_raster(flood_map, buffered, -105.0 - 2 * CELL, 41.0 + 2 * CELL)
    mapper_config = workspace_tmp / "mapper.txt"
    mapper_config.write_text(f"OutFLD\t{flood_map}\n")

    unbuffer_maps(FloodMapperBulkOutput([mapper_config]), workspace)

    cropped = gdal.Open(str(workspace.flood_folder / "GEOGLOWS_dem_ARC_Flood_rp2.tif"))
    assert np.allclose(cropped.GetGeoTransform(), (-105.0, CELL, 0, 41.0, 0, -CELL))
    assert np.array_equal(cropped.ReadAsArray(), buffered[2:12, 2:12])
    assert not flood_map.exists()
