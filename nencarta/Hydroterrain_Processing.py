import os
import math
import logging
import tempfile
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from whitebox import WhiteboxTools

from . import LOG

def _utm_epsg_from_lonlat(lon, lat):
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    return 32600 + zone if lat >= 0 else 32700 + zone


def create_flow_direction_raster(dem: str, filled_dem: str, flowdir_orig: str):
    wbt = WhiteboxTools()
    wbt.set_verbose_mode(LOG.level <= logging.INFO)
    wbt.set_compress_rasters(True)

    with tempfile.TemporaryDirectory() as tmpdir:
        projected_dem = os.path.join(tmpdir, "projected_dem.tif")
        filled_dem_projected = os.path.join(tmpdir, "filled_dem_projected.tif")
        flowdir = os.path.join(tmpdir, "flowdir.tif")

        # Open input DEM
        with rasterio.open(dem) as src:
            if src.crs is None:
                raise ValueError("Input DEM has no CRS defined.")

            src_crs = src.crs
            src_transform = src.transform
            src_width = src.width
            src_height = src.height
            src_profile = src.profile.copy()

            if src.crs.is_geographic:
                center_lon = (src.bounds.left + src.bounds.right) / 2
                center_lat = (src.bounds.bottom + src.bounds.top) / 2
                dst_crs = rasterio.crs.CRS.from_epsg(
                    _utm_epsg_from_lonlat(center_lon, center_lat)
                )

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
                        src_nodata=src.nodata
                    )

                dem_for_routing = projected_dem
            else:
                dem_for_routing = dem
                flowdir = flowdir_orig
                filled_dem_projected = filled_dem

        # Whitebox operations
        wbt.fill_depressions_wang_and_liu(dem_for_routing, filled_dem_projected)
        wbt.d8_pointer(filled_dem_projected, flowdir)

        if flowdir == flowdir_orig:
            # Why reproject if not needed?
            return

        # Reproject flow direction back to original CRS
        with rasterio.open(flowdir) as src:
            profile = src_profile.copy()
            profile.update(
                crs=src_crs,
                transform=src_transform,
                width=src_width,
                height=src_height,
            )

            with rasterio.open(flowdir_orig, "w", **profile) as dst:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=src_transform,
                    dst_crs=src_crs,
                    resampling=Resampling.nearest,
                    src_nodata=src.nodata
                )

        # Reproject filled DEM back to original CRS
        with rasterio.open(filled_dem_projected) as src:
            profile = src_profile.copy()
            profile.update(
                crs=src_crs,
                transform=src_transform,
                width=src_width,
                height=src_height,
            )

            with rasterio.open(filled_dem, "w", **profile) as dst:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=src_transform,
                    dst_crs=src_crs,
                    resampling=Resampling.bilinear,
                    src_nodata=src.nodata
                )
