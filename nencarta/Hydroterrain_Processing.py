# built-in imports
import os
import tempfile
import math

# third-party imports
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from whitebox import WhiteboxTools

from .logger import LOG

def _utm_epsg_from_lonlat(lon, lat):
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    return 32600 + zone if lat >= 0 else 32700 + zone

def create_flow_direction_raster(dem: str, out_dir: str, flowdir_orig: str):
    wbt = WhiteboxTools()
    wbt.set_verbose_mode(False)
    wbt.work_dir = out_dir

    projected_dem = tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name
    filled_dem = tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name
    filled_dem_orig = tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name
    flowdir = tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name

    # Reproject to a projected CRS (meters) before flow routing.
    with rasterio.open(dem) as src:
        if src.crs is None:
            raise ValueError("Input DEM has no CRS defined.")
        src_crs = src.crs
        src_transform = src.transform
        src_width = src.width
        src_height = src.height
        src_profile = src.profile.copy()
        if src.crs.is_geographic:
            center_lon = (src.bounds.left + src.bounds.right) / 2.0
            center_lat = (src.bounds.bottom + src.bounds.top) / 2.0
            dst_crs = rasterio.crs.CRS.from_epsg(_utm_epsg_from_lonlat(center_lon, center_lat))
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds
            )
            profile = src.profile.copy()
            profile.update(
                crs=dst_crs,
                transform=transform,
                width=width,
                height=height,
            )
            with rasterio.open(projected_dem, "w", **profile) as dst:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                    src_nodata=src.nodata,
                    dst_nodata=src.nodata,
                )
            dem_for_routing = projected_dem
        else:
            dem_for_routing = dem

    # Fill pits/depressions so flow routing is continuous.
    wbt.fill_depressions_wang_and_liu(dem_for_routing, filled_dem)

    wbt.d8_pointer(filled_dem, flowdir)

    if dem_for_routing == projected_dem:
        LOG.info(f"Reprojected DEM written to: {projected_dem}")
        
    # Reproject outputs back to the original DEM CRS for consistency.
    for src_path, dst_path, resampling in [
        (flowdir, flowdir_orig, Resampling.nearest),
        (filled_dem, filled_dem_orig, Resampling.nearest),
    ]:
        with rasterio.open(src_path) as src:
            profile = src_profile.copy()
            profile.update(
                crs=src_crs,
                transform=src_transform,
                width=src_width,
                height=src_height,
            )
            with rasterio.open(dst_path, "w", **profile) as dst:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=src_transform,
                    dst_crs=src_crs,
                    resampling=resampling,
                    src_nodata=src.nodata,
                    dst_nodata=src.nodata,
                )

    LOG.info(f"Filled DEM written to: {filled_dem}")
    LOG.info(f"Flow direction written to: {flowdir}")
    if dem_for_routing == projected_dem:
        LOG.info(f"Flow direction (orig CRS) written to: {flowdir_orig}")

    for temp_file in [projected_dem, filled_dem, filled_dem_orig, flowdir]:
        try:
            os.remove(temp_file)
        except OSError:
            pass


