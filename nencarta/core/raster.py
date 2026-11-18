from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from osgeo import gdal, osr
from shapely.geometry import box

from .abc_geo import GISDataSource
from .geo_cache import LMDBCache

gdal.UseExceptions()


class Raster(GISDataSource, LMDBCache):
    """A raster file. Its grid, CRS and bounds are cached (see geo_cache)."""
    def __init__(self, filepath: os.PathLike):
        self.filepath = Path(filepath).resolve()

        LMDBCache.__init__(self)
        self._metadata = self._load_or_compute_metadata()

    @cached_property
    def ds(self) -> gdal.Dataset:
        return gdal.Open(str(self.filepath))

    @property
    def geotransform(self) -> tuple:
        return tuple(self._metadata["geotransform"])

    @property
    def projection(self) -> str:
        return self._metadata["projection"]

    @property
    def shape(self) -> tuple[int, int]:
        """Returns (nrows, ncols)"""
        return tuple(self._metadata["shape"])
    
    @property
    def nrows(self) -> int:
        return self.shape[0]
    
    @property
    def ncols(self) -> int:
        return self.shape[1]

    @property
    def resolution(self) -> tuple[float, float]:
        """Returns (x_resolution, y_resolution)"""
        return tuple(self._metadata["resolution"])

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return tuple(self._metadata["bbox"])

    @property
    def epsg_4326_bbox(self) -> tuple[float, float, float, float]:
        return tuple(self._metadata["epsg_4326_bbox"])

    @property
    def nodata_value(self) -> float | None:
        return self._metadata.get("nodata_value", None)
    
    @property
    def is_in_degrees(self) -> bool:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(self.projection)

        if not srs.IsGeographic():
            return False

        units = srs.GetAngularUnitsName().lower()
        scale = srs.GetAngularUnits()

        return (
            units in {"degree", "degrees"}
            and abs(scale - 0.017453292519943295) < 1e-9
        )
    
    def _load_or_compute_metadata(self) -> dict[str, Any]:
        if not self.can_cache:
            return self._compute_metadata()

        timestamp = self._get_file_timestamp(self.filepath)

        # Fast path
        metadata = self._load_cached_metadata(
            self.filepath,
            timestamp,
        )

        if metadata is not None:
            return metadata

        # Prevent duplicate computation within process threads
        with self._CACHE_LOCK:
            metadata = self._load_cached_metadata(
                self.filepath,
                timestamp,
            )

            if metadata is not None:
                return metadata

            metadata = self._compute_metadata()

            self._save_cached_metadata(
                self.filepath,
                timestamp,
                metadata,
            )

        return metadata
    
    def _compute_metadata(self) -> dict[str, Any]:
        ds = self.ds

        geotransform = ds.GetGeoTransform()
        projection = ds.GetProjection()

        shape = (
            ds.RasterYSize,
            ds.RasterXSize,
        )

        # This takes into account potential rotation terms in the geotransform, but assumes no shearing (i.e. geotransform[2] and geotransform[4] are 0).
        resolution = (
            abs(geotransform[1]),
            abs(geotransform[5]),
        )

        height, width = shape

        minx = geotransform[0]
        maxx = geotransform[0] + width * geotransform[1]
        miny = geotransform[3] + height * geotransform[5]
        maxy = geotransform[3]

        if miny > maxy:
            miny, maxy = maxy, miny

        bbox = (
            minx,
            miny,
            maxx,
            maxy,
        )

        srs = osr.SpatialReference(projection)
        srs.AutoIdentifyEPSG()

        if srs.IsGeographic() and srs.GetAuthorityCode(None) == "4326":
            epsg_4326_bbox = bbox
        else:
            epsg_4326_bbox = tuple(
                gpd.GeoSeries(
                    [box(*bbox)],
                    crs=projection,
                )
                .to_crs("EPSG:4326")
                .total_bounds
            )

        nodata_value = ds.GetRasterBand(1).GetNoDataValue()

        return {
            "geotransform": geotransform,
            "projection": projection,
            "shape": shape,
            "resolution": resolution,
            "bbox": bbox,
            "epsg_4326_bbox": epsg_4326_bbox,
            "nodata_value": nodata_value,
        }

    @classmethod
    def get_rasters_in_extent(cls, epsg_4326_bounds: tuple, other_rasters: list[str]):
        output = set()
        for raster in other_rasters:
            raster_bounds = Raster(raster).epsg_4326_bbox
            if cls.bounds_intersect(epsg_4326_bounds, raster_bounds):
                output.add(raster)

        return list(output)
    
    def read_array(self) -> np.ndarray:
        array = self.ds.ReadAsArray()
        # GDAL keeps the blocks it read cached while the file is open, as a Raster keeps it, up to 5% of the machine's
        # memory by default: now that they're all in the array, they're let go
        self.ds.FlushCache()
        return array
    
    @classmethod
    def write_array_using_reference(cls, 
                          array: np.ndarray, 
                          reference_raster: Raster, 
                          output_path: os.PathLike,
                          output_dtype: int = gdal.GDT_Float32):
        o_driver: gdal.Driver = gdal.GetDriverByName("GTiff")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    
        if os.path.exists(output_path):
            o_driver.Delete(output_path)
            
        # Construct the file with the appropriate data shape
        o_output_file: gdal.Dataset = o_driver.Create(
            output_path, 
            xsize=array.shape[1], 
            ysize=array.shape[0], 
            bands=1, 
            eType=output_dtype, 
            options=['COMPRESS=DEFLATE'])

        # Get the first band (assuming a single-band raster)
        band: gdal.Band = o_output_file.GetRasterBand(1)

        # Initialize the band with zeros
        band.Fill(0)

        # Set the geotransform
        o_output_file.SetGeoTransform(reference_raster.geotransform)
        
        # Set the spatial reference
        o_output_file.SetProjection(reference_raster.projection)
        
        # Write the data to the file
        band.WriteArray(array)

        if reference_raster.nodata_value is not None:
            band.SetNoDataValue(reference_raster.nodata_value)

        # Write to disk
        band.FlushCache()
        
        # Once we're done, close properly the dataset
        o_output_file = None
