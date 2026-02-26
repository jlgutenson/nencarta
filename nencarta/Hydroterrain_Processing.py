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


def create_flow_direction_raster(dem: str, filled_dem: str, out_dir: str, flowdir_orig: str, flowacc_orig: str = None):
    wbt = WhiteboxTools()
    wbt.set_verbose_mode(LOG.level <= logging.INFO)
    wbt.set_compress_rasters(True)
    wbt.work_dir = out_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        projected_dem = os.path.join(tmpdir, "projected_dem.tif")
        filled_dem_projected = os.path.join(tmpdir, "filled_dem_projected.tif")
        flowdir = os.path.join(tmpdir, "flowdir.tif")
        flowacc = os.path.join(tmpdir, "flowacc.tif")

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
                if flowacc_orig:
                    flowacc = flowacc_orig
                filled_dem_projected = filled_dem

        # Whitebox operations
        wbt.fill_depressions_wang_and_liu(dem = dem_for_routing, output = filled_dem_projected, fix_flats=True) # Optionally remove flat areas
        wbt.d8_pointer(dem = filled_dem_projected, output = flowdir)
        if flowacc_orig:
            # Build flow accumulation from the same hydrologically conditioned DEM
            # used for D8 flow direction so both rasters are consistent.
            wbt.d8_flow_accumulation(
                                     filled_dem_projected,
                                     flowacc,
                                     out_type="specific contributing area", # Output type: 'cells' (default), 'catchment area', or 'specific contributing area'
                                     log=False,
                                     clip=False
                                    )

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

        if flowacc_orig:
            # Reproject flow accumulation back to original CRS so it can be used
            # directly by downstream FLDPLN configuration.
            with rasterio.open(flowacc) as src:
                profile = src_profile.copy()
                profile.update(
                    crs=src_crs,
                    transform=src_transform,
                    width=src_width,
                    height=src_height,
                    dtype=src.dtypes[0],
                    nodata=src.nodata,
                )

                with rasterio.open(flowacc_orig, "w", **profile) as dst:
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
