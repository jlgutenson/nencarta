"""
Inputs read the fast way, or only once a run: GeoParquet read within a bbox a batch of rows at a time
(nencarta.core.vector), the lakes and the ocean rasterized once (nencarta.tasks.move_streams), the GEOGLOWS rivers
looked up by position (nencarta.tasks.make_reanalysis_flowfile), and floats written with a predictor. Each must give
exactly what the plain way gives.
"""
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from geopandas.testing import assert_geodataframe_equal
from osgeo import gdal, osr
from shapely.geometry import LineString, box

from nencarta.core import vector
from nencarta.core.raster import Raster
from nencarta.tasks import move_streams
from nencarta.tasks.make_reanalysis_flowfile import (
    _river_ids_in,
    _river_positions,
    _select_rivers,
)

CELL = 30.0
X0, Y0 = 500_000.0, 4_500_000.0
GT = (X0, CELL, 0.0, Y0, 0.0, -CELL)


def utm_wkt() -> str:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32615)
    return srs.ExportToWkt()


def streams_gdf(n: int = 500) -> gpd.GeoDataFrame:
    """``n`` short lines scattered over 10 x 10 degrees, in no spatial order, with a few columns of other types."""
    rng = np.random.default_rng(7)
    x, y = rng.uniform(-110, -100, n), rng.uniform(30, 40, n)
    lines = [LineString([(a, b), (a + 0.05, b + 0.03), (a + 0.08, b - 0.02)]) for a, b in zip(x, y)]
    return gpd.GeoDataFrame({"LINKNO": np.arange(n, dtype=np.int64) + 700_000_000,
                             "strmOrder": rng.integers(1, 8, n).astype(np.int32),
                             "DSContArea": rng.uniform(1e5, 1e10, n),
                             "name": [f"reach {i}" for i in range(n)]},
                            geometry=lines, crs="EPSG:4326")


@pytest.fixture(params=["one row group", "row groups of 64", "an index of its own"])
def geoparquet(request, tmp_path):
    gdf = streams_gdf()
    kwargs = {"write_covering_bbox": True, "index": False}
    if request.param == "row groups of 64":
        kwargs["row_group_size"] = 64
    if request.param == "an index of its own":
        gdf.index = pd.Index(gdf["LINKNO"].to_numpy() * 3, name="key")
        kwargs["index"] = True
    path = tmp_path / "streams.parquet"
    gdf.to_parquet(path, **kwargs)
    return path


@pytest.mark.parametrize("bbox", [(-105, 34, -104, 35), (-120, 50, -119, 51), (-111, 29, -99, 41)],
                         ids=["some rows", "no rows", "every row"])
@pytest.mark.parametrize("columns", [None, ["LINKNO", "geometry"], ["geometry", "bbox"]])
def test_a_geoparquet_file_read_in_batches_within_a_bbox_is_what_geopandas_reads(geoparquet, bbox, columns,
                                                                                monkeypatch):
    # Small batches, so the rows wanted are spread over several
    monkeypatch.setattr(vector, "_PARQUET_BATCH_ROWS", 37)
    expected = gpd.read_parquet(geoparquet, bbox=bbox, columns=columns)
    got = vector._read_parquet_in_bbox(geoparquet, bbox, columns, True)

    assert_geodataframe_equal(got, expected, check_dtype=True, check_index_type=True, check_column_type=True,
                              check_frame_type=True, check_crs=True)
    assert list(got.columns) == list(expected.columns)
    assert (got.geometry.to_wkb() == expected.geometry.to_wkb()).all()


def test_a_geoparquet_file_without_a_bbox_column_is_left_to_geopandas(tmp_path):
    path = tmp_path / "streams.parquet"
    streams_gdf().to_parquet(path, index=False)

    assert vector._read_parquet_in_bbox(path, (-105, 34, -104, 35), None, True) is None


def dem_workspace(tmp_path, **configs):
    """A 40 x 40 DEM, and just what load_lake_array and load_ocean_array read."""
    dem = tmp_path / "dem.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(dem), 40, 40, 1, gdal.GDT_Float32)
    ds.SetGeoTransform(GT)
    ds.SetProjection(utm_wkt())
    ds.GetRasterBand(1).Fill(100.0)
    ds = None
    settings = dict(lakes=None, ocean=None, burn_streams=True, parallel=False, overwrite=True, compression="DEFLATE")
    settings.update(configs)
    workspace = SimpleNamespace(configs=SimpleNamespace(**settings), assigned_dem=dem, lake_raster=tmp_path / "lakes.tif")
    return workspace, Raster(dem)


def polygons(tmp_path, name: str) -> str:
    """Two squares on the DEM, in its CRS."""
    path = tmp_path / f"{name}.gpkg"
    squares = [box(X0 + 5 * CELL, Y0 - 15 * CELL, X0 + 15 * CELL, Y0 - 5 * CELL),
               box(X0 + 20 * CELL, Y0 - 35 * CELL, X0 + 30 * CELL, Y0 - 22 * CELL)]
    gpd.GeoDataFrame(geometry=squares, crs=utm_wkt()).to_file(path)
    return str(path)


def counting(monkeypatch, owner, name):
    calls = []
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, name, counted)
    return calls


def test_with_overwrite_the_lakes_are_rasterized_once_a_run_and_read_back_after(tmp_path, monkeypatch):
    workspace, dem = dem_workspace(tmp_path, lakes=polygons(tmp_path, "lakes"))
    reads = counting(monkeypatch, move_streams, "load_lake_gdf")

    first = move_streams.load_lake_array(workspace, dem)
    again = move_streams.load_lake_array(workspace, dem)

    assert len(reads) == 1
    assert first.dtype == again.dtype == np.bool_
    assert np.array_equal(first, again) and first.sum() == 10 * 10 + 10 * 13
    # A new run (a new workspace) makes them again
    workspace.lake_raster_made = False
    move_streams.load_lake_array(workspace, dem)
    assert len(reads) == 2


def test_the_ocean_is_rasterized_once_for_a_grid_and_shared_read_only(tmp_path, monkeypatch):
    workspace, dem = dem_workspace(tmp_path, ocean=polygons(tmp_path, "ocean"))
    rasterized = counting(monkeypatch, move_streams, "_rasterize_polygons")

    first = move_streams.load_ocean_array(workspace, dem)
    again = move_streams.load_ocean_array(workspace, dem)

    assert len(rasterized) == 1
    assert again is first and not first.flags.writeable
    assert first.sum() == 10 * 10 + 10 * 13
    # Another grid gets its own
    other = tmp_path / "other.tif"
    gdal.Translate(str(other), str(workspace.assigned_dem), srcWin=[0, 0, 20, 20])
    assert move_streams.load_ocean_array(workspace, Raster(other)).shape == (20, 20)
    assert len(rasterized) == 2


def test_an_ocean_off_the_dem_is_none_and_not_read_again(tmp_path, monkeypatch):
    far = tmp_path / "far.gpkg"
    gpd.GeoDataFrame(geometry=[box(0, 0, 10, 10)], crs=utm_wkt()).to_file(far)
    workspace, dem = dem_workspace(tmp_path, ocean=str(far))
    reads = counting(monkeypatch, move_streams, "Vector")

    assert move_streams.load_ocean_array(workspace, dem) is None
    assert move_streams.load_ocean_array(workspace, dem) is None
    assert len(reads) == 1


def flows(river_ids: np.ndarray) -> xr.Dataset:
    rng = np.random.default_rng(3)
    return xr.Dataset({"gumbel": (("return_period", "river_id"), rng.uniform(0, 100, (3, river_ids.size))),
                       "max_simulated": ("river_id", rng.uniform(0, 100, river_ids.size).astype(np.float32))},
                      coords={"return_period": [2, 5, 10], "river_id": river_ids})


@pytest.mark.parametrize("ids", [np.arange(100, 400, 3, dtype=np.int32), np.arange(400, 100, -3, dtype=np.int64)],
                         ids=["sorted", "unsorted"])
def test_rivers_are_selected_by_position_as_by_label(ids):
    ds = flows(ids)
    wanted = np.array([382, 103, 250, 103 + 3 * 7, 388], np.int64)
    wanted = wanted[np.isin(wanted, ids)]

    assert _select_rivers(ds, wanted).identical(ds.sel(river_id=wanted))
    assert _select_rivers(ds["gumbel"], wanted, return_period=[5, 10]).identical(
        ds["gumbel"].sel(return_period=[5, 10], river_id=wanted))


@pytest.mark.parametrize("ids", [np.arange(100, 400, 3, dtype=np.int32), np.arange(400, 100, -3, dtype=np.int64)],
                         ids=["sorted", "unsorted"])
def test_the_rivers_a_dataset_lacks_are_found_by_position_too(ids):
    ds = flows(ids)
    wanted = np.array([103, 104, 99, 397, 401, 1000], np.int64)
    configs = SimpleNamespace(raise_errors_if_river_ids_missing=False)

    kept = _river_ids_in(ds, wanted, "flows", configs)

    assert kept.tolist() == wanted[np.isin(wanted, ids)].tolist()
    with pytest.raises(KeyError):
        _select_rivers(ds, wanted)


def test_rivers_a_dataset_has_twice_are_selected_by_label():
    ids = np.array([5, 9, 7, 9, 11], np.int64)
    ds = flows(ids)

    assert _river_positions(ds, np.array([7, 11])).tolist() == [2, 4]
    assert _river_positions(ds, np.array([7, 9])) is None
    with pytest.raises(Exception):
        _select_rivers(ds, np.array([7, 9]))


@pytest.mark.parametrize("compression", ["ZSTD", "DEFLATE", "LZW"])
@pytest.mark.parametrize("data_type", [gdal.GDT_Float32, gdal.GDT_Float64, gdal.GDT_Int16])
def test_floats_are_written_with_a_predictor_and_read_back_as_they_were(tmp_path, compression, data_type):
    rng = np.random.default_rng(5)
    values = (rng.normal(1500, 300, (64, 80)) if data_type != gdal.GDT_Int16 else rng.integers(-1, 255, (64, 80)))
    values = values.astype(gdal.GetDataTypeName(data_type).lower())
    values[3:9, 4:20] = -9999
    path = tmp_path / "values.tif"

    move_streams._write_raster(path, values, SimpleNamespace(geotransform=GT, projection=utm_wkt()), data_type,
                               -9999, compression)

    ds = gdal.Open(str(path))
    structure = ds.GetMetadata("IMAGE_STRUCTURE")
    assert structure["COMPRESSION"] == compression
    assert structure.get("PREDICTOR") == ("3" if data_type != gdal.GDT_Int16 else None)
    assert np.array_equal(ds.ReadAsArray(), values)
