import os
import time
import urllib
from pathlib import Path
from typing import Literal

os.environ["AWS_NO_SIGN_REQUEST"] = "YES"  # for s3 access to ESA WorldCover data

import geopandas as gpd
from osgeo import gdal
from shapely.geometry import box

from nencarta._constants import CACHED_ESA_GRID
from nencarta.core.raster import Raster
from nencarta.logger import LOG
from nencarta.workspace import Workspace


def _get_esa_tiles(bbox):
    """The names of the ESA WorldCover tiles overlapping ``bbox`` (EPSG:4326), e.g. 'N36W120'."""
    if CACHED_ESA_GRID.exists():
        try:
            return set(gpd.read_parquet(CACHED_ESA_GRID, bbox=bbox)['ll_tile'])
        except ValueError as e:
            if "bbox" in str(e):
                grid = gpd.read_parquet(CACHED_ESA_GRID)
                grid.to_parquet(CACHED_ESA_GRID, write_covering_bbox=True)
                return set(grid[grid.intersects(box(*bbox))]['ll_tile'])

            raise
    
    url = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/esa_worldcover_grid.geojson"
    grid = _download_grid_with_retry(url)
    grid.to_parquet(CACHED_ESA_GRID, write_covering_bbox=True)
    return set(grid[grid.intersects(box(*bbox))]['ll_tile'])

def _download_grid_with_retry(url, max_retries=10, wait_seconds=10):
    """Read the ESA WorldCover tile grid from ``url``, retrying on failure."""
    attempt = 0
    while attempt < max_retries:
        try:
            grid = gpd.read_file(url).to_crs(epsg=4326)
            LOG.info(f"Successfully downloaded grid on attempt {attempt + 1}")
            return grid
        except urllib.error.URLError as e:
            attempt += 1
            LOG.warning(f"Attempt {attempt} failed: {e}")
            time.sleep(wait_seconds)
        except Exception as e:  # noqa: BLE001
            attempt += 1
            LOG.warning(f"Unexpected error on attempt {attempt}: {e}")
            time.sleep(wait_seconds)

    raise RuntimeError(f"Failed to download grid after {max_retries} attempts.")


def make_land_cover(workspace: Workspace,
                    year: Literal[2020, 2021] = 2021):
    """
    Make the land cover raster on the DEM's grid (workspace.LAND_File) from ESA WorldCover tiles, read from
    land_cover_cache or the ESA bucket. Without tiles (e.g. far out at sea) every cell is tree cover (10).
    """
    if workspace.LAND_File.exists() and not workspace.configs.overwrite:
        LOG.info(f"Land cover file already exists at {workspace.LAND_File}, skipping generation.")
        return workspace.LAND_File
    
    workspace.land_folder.mkdir(parents=True, exist_ok=True)

    dem_raster = Raster(workspace.assigned_dem)
    bounds_crs = dem_raster.bbox
    bounds_4326 = dem_raster.epsg_4326_bbox
    tiles = _get_esa_tiles(bbox=bounds_4326)

    version_by_year = {2020: "v100", 2021: "v200"}  # extend as needed for newer years
    if year not in version_by_year:
        raise ValueError(f"Year {year} not supported. Available: {list(version_by_year)}")
    
    version = version_by_year[year]

    landcover_files: list[str] = []
    cached_files = {}
    if workspace.configs.land_cover_cache:
        cached_files = {Path(path).stem: path for path in workspace.configs.land_cover_cache}
        
    # In a fixed order: the tiles are warped one after another, and where two meet, a cell takes the last one's
    for tile in sorted(tiles):
        if tile in cached_files:
            landcover_files.append(cached_files[tile])
        else:
            landcover_files.append(f"/vsis3/esa-worldcover/{version}/{year}/map/ESA_WorldCover_10m_{year}_{version}_{tile}_Map.tif")

    LOG.info(f"Creating {workspace.LAND_File}")
    if not landcover_files:
        out_ds: gdal.Dataset = gdal.GetDriverByName('GTiff').Create(workspace.LAND_File, dem_raster.ncols, dem_raster.nrows, 1, gdal.GDT_Byte, {'COMPRESS': workspace.configs.compression, 'PREDICTOR': '2'})
        out_ds.SetGeoTransform(dem_raster.geotransform)
        out_ds.SetProjection(dem_raster.projection)
        LOG.warning("No land cover files found for the specified DEM extent. Generating a fake land cover file filled with 10 (trees).")
        # Let's make a fake landcover file for arc to use. 
        # Fill it with 10, since the areas that don't have it tend to be tropical islands (10 is trees)
        out_ds.GetRasterBand(1).Fill(10)
        return workspace.LAND_File
    
    proj = dem_raster.projection
    xres, yres = dem_raster.resolution
    remote = all(path.startswith("/vsis3/") for path in landcover_files)
    with gdal.config_options({"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"} if remote else {}):
        if workspace.LAND_File.suffix.lower() == '.vrt':
            options = gdal.BuildVRTOptions(outputBounds=bounds_crs,
                                outputSRS=proj,
                                xRes=xres,
                                yRes=yres,
                                resampleAlg='mode')
            gdal.BuildVRT(workspace.LAND_File, landcover_files, options=options)
        else:
            options = gdal.WarpOptions(format='GTiff',
                                outputBounds=bounds_crs,
                                outputBoundsSRS=proj,
                                dstSRS=proj,
                                xRes=xres,
                                yRes=yres,
                                resampleAlg='mode',
                                creationOptions=[f'COMPRESS={workspace.configs.compression}', 'PREDICTOR=2'])
            gdal.Warp(workspace.LAND_File, landcover_files, options=options)

    return workspace.LAND_File
