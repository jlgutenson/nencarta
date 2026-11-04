from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
from pyproj import CRS
from shapely.geometry import box

from .abc_geo import GISDataSource
from .geo_cache import LMDBCache

# A GeoParquet file read within a bbox is decoded this many rows at a time (see _read_parquet_in_bbox), reading this
# many bytes of the file at a time
_PARQUET_BATCH_ROWS = 1024
_PARQUET_BUFFER_BYTES = 1 << 20


def _read_parquet_in_bbox(path: Path, bbox: tuple, columns: list[str] | None,
                          use_threads: bool) -> gpd.GeoDataFrame | None:
    """
    What ``gpd.read_parquet(path, bbox=bbox, columns=columns)`` reads, decoded a batch of rows at a time, or None when
    the file has no bbox covering column. pyarrow decodes a whole row group before the bbox picks rows out of it, and
    GeoParquet files are often one row group: 4 GB for GLAKES' lakes, to keep the 8 on a 1 degree tile. Here the bbox
    column is read first, so a file with nothing in the bbox isn't decoded, and the rest stops after the last row in it.
    """
    from geopandas.io.arrow import (
        _arrow_to_geopandas,
        _check_if_covering_in_geo_metadata,
        _get_bbox_encoding_column_name,
        _get_non_bbox_columns,
        _get_parquet_bbox_filter,
        _validate_and_decode_metadata,
    )

    parquet_file = pq.ParquetFile(path, buffer_size=_PARQUET_BUFFER_BYTES, pre_buffer=False)
    if parquet_file.metadata.num_rows == 0:
        return None

    schema = parquet_file.schema_arrow
    metadata = schema.metadata
    if metadata is None or b"geo" not in metadata:
        metadata = parquet_file.metadata.metadata
    geo_metadata = _validate_and_decode_metadata(metadata)
    if not _check_if_covering_in_geo_metadata(geo_metadata):
        return None

    # The rows in the bbox, picked with geopandas' own filter
    boxes = parquet_file.read(columns=[_get_bbox_encoding_column_name(geo_metadata)], use_threads=use_threads)
    boxes = boxes.append_column("__row", pa.array(np.arange(boxes.num_rows)))
    rows = boxes.filter(_get_parquet_bbox_filter(geo_metadata, bbox)).column("__row").to_numpy()
    del boxes

    if not columns:
        columns = _get_non_bbox_columns(schema, geo_metadata)
    if rows.size == 0:
        table = parquet_file.read_row_groups([], columns=columns, use_threads=use_threads, use_pandas_metadata=True)
        return _arrow_to_geopandas(table, geo_metadata)

    # Only the row groups with rows in the bbox, and each row's place among their rows
    group_starts = np.cumsum([0] + [parquet_file.metadata.row_group(i).num_rows
                                    for i in range(parquet_file.num_row_groups)])
    of_row = np.searchsorted(group_starts, rows, side="right") - 1
    groups = np.unique(of_row)
    sizes = group_starts[groups + 1] - group_starts[groups]
    rows = rows - group_starts[of_row] + (np.cumsum(sizes) - sizes)[np.searchsorted(groups, of_row)]
    batches, start = [], 0
    for batch in parquet_file.iter_batches(_PARQUET_BATCH_ROWS, row_groups=groups.tolist(), columns=columns,
                                           use_threads=use_threads, use_pandas_metadata=True):
        end = start + batch.num_rows
        first, last = np.searchsorted(rows, (start, end))
        if last > first:
            batches.append(batch.take(pa.array(rows[first:last] - start)))
        start = end
        if start > rows[-1]:
            break

    gdf = _arrow_to_geopandas(pa.Table.from_batches(batches), geo_metadata)
    del batches
    pa.default_memory_pool().release_unused()
    return gdf


class Vector(GISDataSource, LMDBCache):
    """A vector file. Its CRS and bounds are cached (see geo_cache)."""
    def __init__(self, filepath: str, use_threads: bool = True):
        self.filepath = Path(filepath).resolve()
        self.use_threads = use_threads

        LMDBCache.__init__(self)
        self._metadata = self._load_or_compute_metadata()

    @property
    def bbox(self) -> tuple:
        return tuple(self._metadata["bbox"])

    @property
    def epsg_4326_bbox(self) -> tuple:
        return tuple(self._metadata["epsg_4326_bbox"])
    
    @property
    def projection(self) -> str:
        return self._metadata["projection"]

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
        info = pyogrio.read_info(self.filepath, force_total_bounds=True)

        projection = info["crs"]

        # GeoParquet may have CRS metadata even if pyogrio doesn't report it.
        if projection is None:
            if Path(self.filepath).suffix.lower() == ".parquet":
                import pyarrow.parquet as pq

                metadata = pq.ParquetFile(self.filepath).metadata.metadata or {}
                geo = metadata.get(b"geo")
                if geo:
                    crs = json.loads(geo.decode("utf-8"))["columns"]["geometry"].get("crs")
                    if crs and "id" in crs:
                        projection = ":".join(map(str, crs["id"].values()))
                    if crs and not projection:
                        projection = CRS.from_user_input(crs)
            else:
                warnings.warn(f"Could not determine CRS for {self.filepath}. Assuming EPSG:4326.", stacklevel=2)
                projection = CRS.from_epsg(4326)

        bbox = info["total_bounds"]

        epsg_4326_bbox = bbox
        if projection is not None:
            crs = CRS.from_user_input(projection)
            if crs != CRS.from_epsg(4326):
                epsg_4326_bbox = (
                    gpd.GeoSeries([box(*bbox)], crs=crs)
                    .to_crs(4326)
                    .total_bounds
                )

        return {
            "projection": projection,
            "bbox": bbox,
            "epsg_4326_bbox": epsg_4326_bbox,
        }

    def to_geopandas(self, bbox_epsg_4326: list[float] | None = None, columns: list[str] | None = None) -> gpd.GeoDataFrame:
        """Read the file (shapefile, GeoPackage, GeoJSON, GeoParquet, ...), or the part in a bbox (EPSG:4326)."""
        if bbox_epsg_4326 is not None:
            # If bbox is provided, we need to make sure it's in the same CRS as the data. We can check the CRS of the data by reading just the metadata with pyogrio, and then reprojecting the bbox if necessary.
            data_crs = self.projection
            if data_crs is not None and data_crs != 'EPSG:4326':
                minx, miny, maxx, maxy = bbox_epsg_4326
                gdf_bbox = gpd.GeoSeries([box(minx, miny, maxx, maxy)], crs="EPSG:4326").to_crs(data_crs)
                bbox_epsg_4326 = tuple(gdf_bbox.total_bounds)

        if self.filepath.suffix.lower() in ('.parquet', '.geoparquet'):
            if bbox_epsg_4326 is not None:
                gdf = _read_parquet_in_bbox(self.filepath, bbox_epsg_4326, columns, self.use_threads)
                if gdf is not None:
                    return gdf
            try:
                return gpd.read_parquet(self.filepath, bbox=bbox_epsg_4326, columns=columns, use_threads=self.use_threads)
            except ValueError as e:
                if "Specifying 'bbox' not supported for this Parquet file" in str(e):
                    # warnings.warn(f"Could not read {self.filepath} with bbox. Consider adding covering bbox to parquet file for faster reading.", stacklevel=2)
                    gdf = gpd.read_parquet(self.filepath, columns=columns, use_threads=self.use_threads)
                    minx, miny, maxx, maxy = bbox_epsg_4326
                    gdf = gdf.cx[minx:maxx, miny:maxy]
                    return gdf
                raise
            
        if self.filepath.suffix.lower() == ".gdb":
            # Specify the layer you want to access
            layer = "geoglowsv2"
        else:
            layer = None
        
        return gpd.read_file(self.filepath, use_arrow=True, bbox=bbox_epsg_4326, columns=columns, layer=layer)
    
    @classmethod
    def save_any_geom(cls, gdf: gpd.GeoDataFrame, path: os.PathLike, **kwargs) -> None:
        cls.delete_any_geom(path)
        if Path(path).suffix.lower() in ('.parquet', '.geoparquet'):
            gdf.to_parquet(path, **kwargs)
        else:
            gdf.to_file(path, **kwargs)

    @classmethod
    def delete_any_geom(cls, path: os.PathLike) -> None:
        path = Path(path).resolve()
        if not path.exists():
            return

        if path.suffix.lower() == ".shp":
            shapefile = Path(path)
            for sidecar in shapefile.parent.glob(f"{shapefile.stem}.*"):
                sidecar.unlink()
            return

        path.unlink()

    @classmethod
    def _streamline_is_in_dem_bounds(cls, stream: str, dem_bounds: tuple[float, float, float, float]) -> bool:
        stream_bounds = Vector(stream).epsg_4326_bbox

        return cls.bounds_intersect(stream_bounds, dem_bounds)

    @classmethod
    def get_streamlines_in_extent(cls, bounds: tuple[float, float, float, float], streamlines: list[str]) -> list[str]:
        """The stream files whose bounds intersect ``bounds`` (EPSG:4326)."""
        streamlines_to_clip = []
        for stream in streamlines:
            if cls._streamline_is_in_dem_bounds(stream, bounds):
                streamlines_to_clip.append(stream)

        return streamlines_to_clip
