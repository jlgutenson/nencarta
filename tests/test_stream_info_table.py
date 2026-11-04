"""
The FLDPLN spreader in Curve2Flood reads each row of the stream info table as a D8
walk: it starts at ``start_pixel``, steps ``length`` pixels down the flow direction
raster, and treats what it visits as that reach's stream pixels, then traces
downstream from ``end_pixel``. These tests pin that contract.
"""

import numpy as np
import pandas as pd
import pytest
from osgeo import gdal

from nencarta.tasks.move_streams import _create_stream_info_table
from nencarta.tasks.stream_conflation import D8_OFFSETS as _D8_OFFSETS


def write_flowdir(path, array):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, gdal.GDT_Int16)
    ds.WriteArray(array)
    ds.GetRasterBand(1).SetNoDataValue(-32768)
    ds = None


def walk(start, length, flowdir, shape):
    """Reproduce Curve2Flood's ``_segment_pixels``: ``length`` pixels down the D8 path."""
    nrows, ncols = shape
    pixels = [start]
    for _ in range(1, length):
        row, col = divmod(pixels[-1], ncols)
        d_row, d_col = _D8_OFFSETS.get(int(flowdir[row, col]), (0, 0))
        if (d_row, d_col) == (0, 0):
            break
        row += d_row
        col += d_col
        if not (0 <= row < nrows and 0 <= col < ncols):
            break
        pixels.append(row * ncols + col)
    return pixels


@pytest.fixture
def straight_reach(workspace_tmp):
    """One reach running east along row 2, then draining off the east edge."""
    streams = np.zeros((5, 8), np.int32)
    streams[2, 1:6] = 77
    flowdir = np.zeros((5, 8), np.int16)
    flowdir[2, :] = 2  # Whitebox code 2 == flow to the east
    write_flowdir(workspace_tmp / "flowdir.tif", flowdir)
    return streams, flowdir, workspace_tmp / "flowdir.tif"


def test_walk_from_start_lands_on_end(workspace_tmp, straight_reach):
    streams, flowdir, flowdir_file = straight_reach
    out = workspace_tmp / "stream_info.parquet"

    _create_stream_info_table(out, streams, flowdir_file)

    row = pd.read_parquet(out).iloc[0]
    assert row.stream_id == 77
    assert row.start_pixel == 2 * 8 + 1
    assert row.end_pixel == 2 * 8 + 5
    assert row.length == 5
    assert walk(row.start_pixel, row.length, flowdir, streams.shape)[-1] == row.end_pixel


def test_reach_is_oriented_downstream_whatever_the_link_numbering(workspace_tmp):
    """A reach is oriented by the flow direction raster, not by how it was digitised."""
    streams = np.zeros((5, 8), np.int32)
    streams[2, 1:6] = 77
    flowdir = np.zeros((5, 8), np.int16)
    flowdir[2, :] = 32  # code 32 == flow to the west, so the reach runs the other way
    write_flowdir(workspace_tmp / "flowdir.tif", flowdir)
    out = workspace_tmp / "stream_info.parquet"

    _create_stream_info_table(out, streams, workspace_tmp / "flowdir.tif")

    row = pd.read_parquet(out).iloc[0]
    assert row.start_pixel == 2 * 8 + 5
    assert row.end_pixel == 2 * 8 + 1
    assert row.length == 5


def test_length_counts_d8_steps_not_rasterized_cells(workspace_tmp):
    """
    A rasterized diagonal line carries cells the D8 path never steps on. ``length``
    has to be the number of steps, or the walk overshoots the end of the reach.
    """
    streams = np.zeros((6, 6), np.int32)
    for i in range(4):
        streams[i, i] = 77
        streams[i, i + 1] = 77  # the extra cell a rasterized diagonal picks up
    flowdir = np.zeros((6, 6), np.int16)
    flowdir[:, :] = 4  # code 4 == flow to the south-east
    write_flowdir(workspace_tmp / "flowdir.tif", flowdir)
    out = workspace_tmp / "stream_info.parquet"

    _create_stream_info_table(out, streams, workspace_tmp / "flowdir.tif")

    row = pd.read_parquet(out).iloc[0]
    assert int(streams.sum() / 77) == 8  # eight cells carry the link number...
    assert row.length == 4               # ...but the D8 path only steps through four
    assert walk(row.start_pixel, row.length, flowdir, streams.shape)[-1] == row.end_pixel


def test_every_reach_gets_a_row_and_every_row_is_walkable(workspace_tmp):
    """Two reaches meeting at a confluence, plus a reach that is off the flow network."""
    streams = np.zeros((10, 10), np.int32)
    streams[3, 1:5] = 11        # tributary flowing east into...
    streams[3, 5:9] = 22        # ...the main stem
    streams[7, 2:5] = 33        # a reach the flow direction raster knows nothing about
    flowdir = np.zeros((10, 10), np.int16)
    flowdir[3, :] = 2
    write_flowdir(workspace_tmp / "flowdir.tif", flowdir)
    out = workspace_tmp / "stream_info.parquet"

    _create_stream_info_table(out, streams, workspace_tmp / "flowdir.tif")

    table = pd.read_parquet(out).set_index("stream_id")
    assert sorted(table.index) == [11, 22, 33]
    for stream_id, row in table.iterrows():
        pixels = walk(row.start_pixel, row.length, flowdir, streams.shape)
        assert len(pixels) == row.length, f"reach {stream_id} walk was cut short"
        assert pixels[-1] == row.end_pixel, f"reach {stream_id} does not end on end_pixel"
        assert streams.ravel()[row.start_pixel] == stream_id
        assert streams.ravel()[row.end_pixel] == stream_id
    assert table.loc[33, "length"] == 1  # nothing to trace, so it is a single pixel


def test_csv_output(workspace_tmp, straight_reach):
    streams, _, flowdir_file = straight_reach
    out = workspace_tmp / "stream_info.csv"

    _create_stream_info_table(out, streams, flowdir_file)

    assert pd.read_csv(out).columns.tolist() == ["start_pixel", "end_pixel", "length", "stream_id"]


def test_missing_flow_direction_raster_is_reported(workspace_tmp, straight_reach):
    streams, _, _ = straight_reach
    with pytest.raises(FileNotFoundError, match="Flow direction raster"):
        _create_stream_info_table(workspace_tmp / "stream_info.parquet", streams, workspace_tmp / "nope.tif")


def test_mismatched_grid_is_reported(workspace_tmp, straight_reach):
    streams, _, flowdir_file = straight_reach
    with pytest.raises(ValueError, match="DEM grid"):
        _create_stream_info_table(workspace_tmp / "stream_info.parquet", streams[:, :4], flowdir_file)
