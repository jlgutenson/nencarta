"""
Burning the source streams into the DEM, and moving them onto its D8 flow paths (nencarta.tasks.move_streams).

The burn has one job the D8 flow paths depend on: every burned river has to keep draining downstream. Where it
doesn't, fill turns the burned channel above the blockage into a flat pool and the flow paths across it run in
straight lines.
"""
import heapq
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from osgeo import gdal, osr
from shapely.geometry import LineString, MultiLineString, box

from nencarta.core.configs import NencartaConfig
from nencarta.core.raster import Raster
from nencarta.tasks.make_bathy_water_mask import (
    make_water_mask,
    near_the_rivers_level,
    one_cell_wide_where_mostly,
    water_mask,
)
from nencarta.tasks.move_streams import (
    FLOWDIR_NODATA,
    ROUTING_MAX_DROP_M,
    _burn_linestring,
    _cut_at_the_sea,
    _get_stream_raster,
    _reaches_in_the_sea,
    _reaches_left_out,
    _reaches_stuck_in_lakes,
    _trunks,
    _without_reaches,
    burn_streams_and_move_streams,
    burn_streams_into_dem,
    fill_and_flow_directions,
    fill_depressions,
    load_ocean_array,
    restore_unfollowed_burns,
    routing_dem,
    smooth_burned_dem,
)
from nencarta.tasks.stream_conflation import D8_OFFSETS
from nencarta.workspace import Workspace

CELL = 30.0
X0, Y0 = 500_000.0, 4_500_000.0
GT = (X0, CELL, 0.0, Y0, 0.0, -CELL)


def utm_wkt() -> str:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32615)
    return srs.ExportToWkt()


def centre(r, c):
    return X0 + (c + 0.5) * CELL, Y0 - (r + 0.5) * CELL


def mem_dataset(array: np.ndarray, nodata: float = -9999.0) -> gdal.Dataset:
    ds = gdal.GetDriverByName("MEM").Create("", array.shape[1], array.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform(GT)
    ds.SetProjection(utm_wkt())
    ds.GetRasterBand(1).WriteArray(array)
    ds.GetRasterBand(1).SetNoDataValue(nodata)
    return ds


def write_raster(path, array, dtype=gdal.GDT_Float32, nodata=None):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, dtype)
    ds.SetGeoTransform(GT)
    ds.SetProjection(utm_wkt())
    ds.GetRasterBand(1).WriteArray(array)
    if nodata is not None:
        ds.GetRasterBand(1).SetNoDataValue(nodata)
    ds = None


def filled(dem: np.ndarray) -> np.ndarray:
    """A priority-flood depression fill draining off every edge (what RichDEM does before D8)."""
    nrows, ncols = dem.shape
    out = dem.astype(np.float64).copy()
    done = np.zeros(dem.shape, bool)
    heap = []
    for r in range(nrows):
        for c in range(ncols):
            if r in (0, nrows - 1) or c in (0, ncols - 1):
                heapq.heappush(heap, (out[r, c], r, c))
                done[r, c] = True
    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc in D8_OFFSETS.values():
            nr, nc = r + dr, c + dc
            if 0 <= nr < nrows and 0 <= nc < ncols and not done[nr, nc]:
                done[nr, nc] = True
                out[nr, nc] = max(out[nr, nc], z)
                heapq.heappush(heap, (out[nr, nc], nr, nc))
    return out


# ---------------------------------------------------------------------------------------------------------------
# The burn
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def flat_river():
    """
    A 30 x 40 tile with a river's flattened water surface at 141.2 m across rows 10 to 20 and land at 150 m. The
    river (row 15, flowing east off the edge) is split at column 20, where a tributary from the north joins it,
    and the network lists the tributary last, so drawn in table order it would own the cell where the three meet.
    A bank cell at 135 m near the river's upstream end drags the whole burned channel below the water surface,
    as FABDEM's flattened rivers do.
    """
    dem = np.full((30, 40), 150.0, np.float32)
    dem[10:21, :] = 141.2
    dem[14, 1] = 135.0
    for r in range(10):
        dem[r, 18:23] = 146.0 - 0.4 * r  # the tributary's valley
    upper = LineString([centre(15, c) for c in range(21)][::-1])   # drawn downstream to upstream, as TDX-Hydro
    lower = LineString([centre(15, c) for c in range(20, 40)][::-1])
    tributary = LineString([centre(r, 20) for r in range(16)][::-1])
    gdf = gpd.GeoDataFrame({"LINKNO": [3, 1, 2], "DSLINKNO": [-1, 3, 3], "strmOrder": [2, 2, 1],
                            "DSContArea": [900.0, 850.0, 20.0]},
                           geometry=[lower, upper, tributary], crs="EPSG:32615")
    channel_mask = np.zeros(dem.shape, bool)
    channel_mask[10:21, :] = True
    return dem, gdf, channel_mask


def burn(dem, gdf, channel_mask, order_col="strmOrder", area_col="DSContArea", paths=None, **rules):
    dem = dem.copy()
    dem_ds = mem_dataset(dem)
    streams = _get_stream_raster(dem_ds, gdf, "LINKNO", order_col, area_col)
    burn_streams_into_dem(channel_mask.copy(), dem, streams, dem_ds, gdf, None, "LINKNO", "DSLINKNO", -9999.0,
                          order_col=order_col, area_col=area_col, paths=paths, **rules)
    return dem, streams


def test_the_larger_reach_owns_the_cell_where_reaches_meet(flat_river):
    dem, gdf, _ = flat_river
    streams = _get_stream_raster(mem_dataset(dem), gdf, "LINKNO", "strmOrder", "DSContArea")
    assert streams[15, 20] == 3  # the river below the confluence, not the tributary listed after it

    # Without order or area to go on, the table order still decides
    streams = _get_stream_raster(mem_dataset(dem), gdf, "LINKNO")
    assert streams[15, 20] == 2


def test_a_burned_river_keeps_draining_through_a_confluence(flat_river):
    dem, gdf, channel_mask = flat_river
    burned, streams = burn(dem, gdf, channel_mask)

    river = [(15, c) for c in range(40)]
    profile = np.array([burned[cell] for cell in river])
    assert (np.diff(profile[1:]) <= 0).all(), profile
    # Nothing along the river is a pit that filling would flatten
    assert np.allclose(filled(burned)[15, :], burned[15, :])


def test_a_one_cell_gap_in_the_channel_mask_is_burned_through(flat_river):
    # The channel mask is made from the cleaned stream raster, which can leave out a cell the burn's own stream
    # raster has. The burn used to skip such a cell, leaving it at the water's surface: a dam.
    dem, gdf, channel_mask = flat_river
    channel_mask[15, 30] = False
    burned, _ = burn(dem, gdf, channel_mask)

    assert burned[15, 30] <= burned[15, 29]
    assert np.allclose(filled(burned)[15, :], burned[15, :])


def test_a_line_whose_vertices_skip_a_cell_burns_the_cells_between_them():
    # A line with a vertex every cell's length in metres skips columns where cells are narrower than that, as a
    # redrawn line does at 40 N: the skipped cells are burned too, or each one dams the channel
    dem = np.full((5, 12), 15.0)
    dem[2, :] = 10.0
    dem[2, 5] = 20.0  # a cell the line's vertices step over
    streams = np.zeros(dem.shape, np.int32)
    streams[2, :] = 1
    coords = np.array([(col + 0.5, 2.5) for col in range(0, 12, 2)])
    inverse = gdal.InvGeoTransform((0.0, 1.0, 0.0, 5.0, 0.0, -1.0))
    _burn_linestring(dem, streams, coords, 1, tuple(inverse), 2, 0, np.inf, -9999.0, np.ones(dem.shape[0]), 1.0)
    assert (dem[2, :11] <= 10).all()


def chain_on_flat_water(reaches=8, length=10, water=141.2):
    """
    A river flowing east along row 10 of a 20-row tile, on flat water at ``water`` m (rows 6 to 14) between land at
    150 m, split into ``reaches`` reaches of ``length`` cells, each flowing into the next.
    """
    cols = reaches * length
    dem = np.full((20, cols), 150.0, np.float32)
    dem[6:15] = water
    lines = [LineString([centre(10, c) for c in range(i * length, min((i + 1) * length + 1, cols))][::-1])
             for i in range(reaches)]
    ids = list(range(1, reaches + 1))
    gdf = gpd.GeoDataFrame({"LINKNO": ids, "DSLINKNO": ids[1:] + [-1], "strmOrder": [3] * reaches,
                            "DSContArea": [1e9 + i for i in range(reaches)]}, geometry=lines, crs="EPSG:32615")
    channel_mask = np.zeros(dem.shape, bool)
    channel_mask[6:15] = True
    return dem, gdf, channel_mask


def test_a_river_on_flat_water_isnt_lowered_at_every_junction():
    # The burn used to lower each reach's last cell 0.5 m wherever no cell beside the line was as low as it, which
    # once the line is below the water is everywhere, and each reach starts no higher than the one above it: 0.5 m
    # deeper at every junction. On FABDEM's Mississippi this put the river 8 m below its water.
    dem, gdf, channel_mask = chain_on_flat_water()
    burned, _ = burn(dem, gdf, channel_mask)

    river = burned[10]
    assert np.all(np.diff(river) <= 0)
    # 0.5 m quantisation, and the 0.5 m drop where the river leaves the tile
    assert np.all(river[:-1] == 141.0), river
    assert river[-1] == 140.5


@pytest.fixture
def tributary_below_the_river():
    """
    The river of chain_on_flat_water (two reaches, split at column 30), on water at 141.2 m, and a tributary from
    the north along column 30 whose bed runs down to 139.1 m at its mouth, below the river's water, as tributaries
    reaching FABDEM's flattened rivers often do.
    """
    dem, gdf, channel_mask = chain_on_flat_water(reaches=2, length=30)
    dem[0:6, 30] = 140.0 - 0.15 * np.arange(6)
    dem[0:6, 29] = dem[0:6, 31] = 145.0
    tributary = LineString([centre(r, 30) for r in range(11)][::-1])
    gdf = pd.concat([gdf.assign(DSLINKNO=[2, -1]),
                     gpd.GeoDataFrame({"LINKNO": [3], "DSLINKNO": [2], "strmOrder": [1], "DSContArea": [5e6]},
                                      geometry=[tributary], crs="EPSG:32615")], ignore_index=True)
    return dem, gdf, channel_mask


def test_a_tributary_below_the_river_doesnt_lower_it(tributary_below_the_river):
    dem, gdf, channel_mask = tributary_below_the_river
    burned, streams = burn(dem, gdf, channel_mask)

    # The river below the junction starts from the river above it, not from the tributary: it stays at its water
    assert np.all(burned[10, :-1] == 141.0), burned[10]
    # The tributary still never rises, and is left in a pool at its mouth, as the river's backwater would leave it
    tributary = burned[:10, 30][streams[:10, 30] == 3]
    assert np.all(np.diff(tributary) <= 0) and tributary[-1] < 141.0


def test_without_reach_sizes_the_river_is_lowered_to_drain_every_tributary(tributary_below_the_river):
    # Without stream orders or areas the burn can't tell the river from the tributary, so it keeps both draining
    dem, gdf, channel_mask = tributary_below_the_river
    burned, streams = burn(dem, gdf, channel_mask, order_col=None, area_col=None)

    mouth = burned[:10, 30][streams[:10, 30] == 3][-1]
    assert np.all(burned[10, 31:] <= mouth)
    assert np.allclose(filled(burned)[:10, 30], burned[:10, 30])


def test_a_line_across_a_flat_pool_keeps_falling_from_the_edge_it_enters_over():
    # FABDEM flattens a pool into one level, so a river burned across it is level too, and a level line can drain
    # either way: the flow directions took the Illinois back out over the DEM's edge it came in over. Smoothed, the
    # line keeps falling all the way across the pool to where the river leaves the tile.
    dem, gdf, channel_mask = chain_on_flat_water(reaches=2, length=30, water=127.0)
    dem[6:15, 40:] = 150.0  # the river leaves the pool, and carries on across land at the pool's level
    channel_mask[:, 40:] = False
    paths = []
    burned, streams = burn(dem, gdf, channel_mask, paths=paths)
    smoothed = smooth_burned_dem(burned.copy(), channel_mask | (streams > 0), streams, dem, -9999.0, paths=paths)

    river = smoothed[10]
    # Level only where the reaches meet, the lower one starting at the upper one's end: no flat to drain backwards
    assert np.all(np.diff(river) <= 0) and np.all(river[2:] < river[:-2]), river
    # Below the water's middle, and within a few decimetres of it
    assert np.all(river[:40] <= 127.0 - 0.1) and np.all(river[:40] > 127.0 - 0.5)


def test_the_burn_leaves_a_line_on_the_dems_leveled_water():
    # FABDEM levels the river's water at 141 m; a cell at 135 m beside the line near its start, as in flat_river,
    # used to drag the whole burned river 6 m below its water
    dem = np.full((20, 40), 150.0, np.float32)
    dem[6:15] = 141.0
    dem[9, 1] = 135.0
    # Drawn upstream to downstream: on level water, the burn can only tell its upstream end by the drawing
    gdf = gpd.GeoDataFrame({"LINKNO": [1], "DSLINKNO": [-1], "strmOrder": [1], "DSContArea": [1e7]},
                           geometry=[LineString([centre(10, c) for c in range(40)])], crs="EPSG:32615")
    leveled = dem == 141.0
    burned, _ = burn(dem, gdf, leveled, leveled=leveled)
    assert np.all(burned[10, :39] == 141.0) and burned[10, 39] == 140.5

    burned, _ = burn(dem, gdf, leveled)
    assert np.all(burned[10, 2:] <= 135.0)


EAST = next(code for code, offset in D8_OFFSETS.items() if tuple(offset) == (0, 1))


def dammed_line(ditch: bool = False):
    """
    A line along row 10 crossing land at 142 m (columns 0-9), where a pit beside it at 139 m drags it down, before the
    water the DEM levels at 141 m, burned: the burn leaves the line on the water, at 141 m, so the water dams it 2 m.
    With ``ditch``, a ditch at 140 m runs from the pit to the DEM's northern edge. Returns the burned DEM and its path.
    """
    dem = np.full((20, 40), 150.0, np.float32)
    dem[6:15] = 141.0
    dem[6:15, :10] = 142.0
    dem[9, 3] = 139.0
    if ditch:
        dem[:9, 3] = 140.0
    gdf = gpd.GeoDataFrame({"LINKNO": [1], "DSLINKNO": [-1], "strmOrder": [1], "DSContArea": [1e7]},
                           geometry=[LineString([centre(10, c) for c in range(40)])], crs="EPSG:32615")
    leveled = dem == 141.0
    paths = []
    burned, _ = burn(dem, gdf, leveled, paths=paths, leveled=leveled)
    assert burned[10, 5] <= 139.0 and np.all(burned[10, 10:39] == 141.0)
    return burned, paths


def test_the_flow_directions_dont_see_the_water_dam_a_line_cut_below_it():
    # Filled, the line pooled behind the water, and its flow left it across the pool (760390495 on the HPC's N35W090
    # tile). The DEM the flow directions come from is the burned DEM filled, with the line run down the fill, so the
    # line is the lowest way across the pool; ARC keeps the line on the water
    burned, paths = dammed_line()
    assert filled(burned)[10, 5] > burned[10, 5]

    routing = routing_dem(burned, -9999.0, paths, [(1, [])], GT, utm_wkt())
    _, flowdir = fill_and_flow_directions(routing, routing != -9999.0, GT, utm_wkt())
    assert np.all(flowdir[10, :39] == EAST), flowdir[10]
    line = routing[10]
    assert np.all(np.diff(line) < 0), line
    assert np.allclose(filled(routing)[10], line)
    # The pool spills along the line, at the water's level, so the line isn't cut below the water. Lowered to its
    # lowest point so far (139 m beside the pit) instead, it was carried that low to the DEM's edge, and on flat
    # ground across whole networks (the south Florida tile, N26W081)
    assert np.all(line[10:39] > 141.0 - 0.01), line
    # Off the line, the DEM is filled as the flow directions fill it
    assert np.allclose(np.delete(routing, 10, axis=0), np.delete(filled(burned), 10, axis=0))


def test_a_line_is_cut_through_the_water_damming_it_only_to_where_its_pool_spills():
    # The pool behind the water spills down a ditch from the pit to the DEM's edge, at 140 m, a metre below the
    # water: filled, the flow left the line down the ditch. Run down the fill, the line is cut through the water to
    # 140 m, where its pool spills, and no lower: not to 139 m, its lowest point above
    burned, paths = dammed_line(ditch=True)
    assert np.isclose(filled(burned)[10, 5], 140.0) and np.isclose(filled(burned)[10, 20], 141.0)

    routing = routing_dem(burned, -9999.0, paths, [(1, [])], GT, utm_wkt())
    _, flowdir = fill_and_flow_directions(routing, routing != -9999.0, GT, utm_wkt())
    assert np.all(flowdir[10, :39] == EAST), flowdir[10]
    line = routing[10]
    assert np.all(line[10:] < 140.0) and np.all(line[10:] > 140.0 - 0.01), line


def test_a_line_starts_below_where_a_relocated_feeder_comes_nearest_it_not_below_its_end():
    # Reach 1 comes down row 10 to its junction at column 20, where reach 2 goes on down row 10, but relocated, its
    # line runs on past the junction to a lower valley along row 14, 13 m below reach 2's start (710839420 and
    # 710948539 in Salt Lake City, n40w112). Started below reach 1's end, reach 2 was cut to that level all its
    # length; started below where reach 1 comes nearest its start, the junction, it follows its own fill
    dem = np.full((20, 40), 160.0, np.float32)
    dem[10] = 150.0 - 0.25 * np.arange(40)
    dem[11:14, 20] = 145.0
    dem[14, 20:] = 130.0 - 0.1 * np.arange(20)
    flat = lambda cells: np.array([r * 40 + c for r, c in cells], np.int64)
    feeder = flat([(10, c) for c in range(21)] + [(r, 20) for r in range(11, 14)] + [(14, c) for c in range(20, 36)])
    river = flat([(10, c) for c in range(21, 40)])
    paths = [(np.empty(0, np.int64), feeder), (np.empty(0, np.int64), river)]

    routing = routing_dem(dem, -9999.0, paths, [(1, []), (2, [1])], GT, utm_wkt())
    assert routing[14, 35] < 129.0
    drop = filled(dem)[10, 21:39] - routing[10, 21:39]
    assert np.all((drop >= 0) & (drop <= ROUTING_MAX_DROP_M)), drop


def test_a_line_on_the_water_is_kept_below_the_water_beside_it():
    # The water falls toward its middle (row 10), as the smoothing leaves it, and the line runs along row 8, off the
    # middle, as the Mississippi's on the HPC's N35W090 tile. Filled, the line drained into the middle and its flow
    # ran down that instead. Run down the fill, each line cell is low enough for the one above to drain into it
    # rather than beside it: by millimetres here, and never more than ROUTING_MAX_DROP_M
    dem = np.full((20, 40), 150.0, np.float32)
    for r in range(4, 17):
        dem[r] = 141.0 - 0.05 * (1 - abs(r - 10) / 7) - 0.001 * np.arange(40)
    _, plain = fill_and_flow_directions(dem, np.ones(dem.shape, bool), GT, utm_wkt())
    assert not np.any(plain[8, :39] == EAST)

    cells = np.array([8 * 40 + c for c in range(40)], np.int64)
    routing = routing_dem(dem, -9999.0, [(np.empty(0, np.int64), cells)], [(1, [])], GT, utm_wkt())
    _, flowdir = fill_and_flow_directions(routing, routing != -9999.0, GT, utm_wkt())
    assert np.all(flowdir[8, :39] == EAST), flowdir[8]
    drop = filled(dem)[8] - routing[8]
    assert np.all((drop >= 0) & (drop < 0.01)) and ROUTING_MAX_DROP_M > 0.01, drop


def test_a_line_the_burn_left_on_the_water_is_smoothed_as_the_water_around_it():
    # FABDEM levels the river's water at 141 m across rows 6-14, and the line runs along row 8, off its middle.
    # Smoothed, the line isn't lowered to the water's middle: it is the same as the water at row 12, as far from
    # the other bank
    dem = np.full((20, 40), 150.0, np.float32)
    dem[6:15] = 141.0
    gdf = gpd.GeoDataFrame({"LINKNO": [1], "DSLINKNO": [-1], "strmOrder": [1], "DSContArea": [1e7]},
                           geometry=[LineString([centre(8, c) for c in range(40)])], crs="EPSG:32615")
    leveled = dem == 141.0
    paths, kept = [], np.zeros(dem.shape, bool)
    burned, streams = burn(dem, gdf, leveled, paths=paths, leveled=leveled, kept=kept)
    smoothed = smooth_burned_dem(burned.copy(), leveled, streams, dem, -9999.0, paths=paths, fixed=kept)
    assert np.allclose(smoothed[8, 2:38], smoothed[12, 2:38])
    assert smoothed[10, 20] < smoothed[8, 20] < 141.0  # the water's middle is lower still
    assert smoothed[8, 39] <= 140.5  # its end at the DEM's edge stays as low as the burn put it


def test_the_smoothing_leaves_the_cells_the_burn_left():
    # A line along a valley's river (row 10) starts no higher than the reach flowing into it, which ends 2 m lower,
    # at (9, 0). Smoothed, the line is lowered to fall from there, but not where the burn left it, from column 5
    rows, cols = np.mgrid[0:20, 0:40]
    dem = (100.013 - 0.05 * cols + 0.5 * np.abs(rows - 10)).astype(np.float32)
    dem[9, 0] = dem[10, 0] - 2.0
    streams = np.zeros(dem.shape, np.int32)
    streams[10] = 1
    paths = [(np.array([9 * 40]), np.arange(10 * 40, 10 * 40 + 39))]
    fixed = np.zeros(dem.shape, bool)
    fixed[10, 5:] = True
    smoothed = smooth_burned_dem(dem.copy(), np.zeros(dem.shape, bool), streams, dem, -9999.0, paths=paths,
                                 fixed=fixed)
    assert np.all(smoothed[10, :5] < dem[10, :5])
    assert np.array_equal(smoothed[10, 5:39], dem[10, 5:39])

    smoothed = smooth_burned_dem(dem.copy(), np.zeros(dem.shape, bool), streams, dem, -9999.0, paths=paths)
    assert np.all(smoothed[10, 5:39] < dem[10, 5:39])


def test_a_reach_with_nothing_up_or_downstream_is_burned():
    dem = np.full((20, 20), 150.0, np.float32)
    dem[:, 10] = 140.3
    gdf = gpd.GeoDataFrame({"LINKNO": [7], "DSLINKNO": [-1]},
                           geometry=[LineString([centre(r, 10) for r in range(20)])], crs="EPSG:32615")
    burned, _ = burn(dem, gdf, np.zeros(dem.shape, bool), order_col=None, area_col=None)

    assert (burned[1:19, 10] <= 140.0).all()


def line_east_along_row(row, cols):
    """One reach drawn west to east along ``row``, with nothing up or downstream of it."""
    return gpd.GeoDataFrame({"LINKNO": [1], "DSLINKNO": [-1]},
                            geometry=[LineString([centre(row, c) for c in range(cols)])], crs="EPSG:32615")


def steepest_way_down(dem, r, c):
    """The cell next to (r, c) the D8 flow directions take on square cells, the steepest way down, or None."""
    best, cell = 0.0, None
    for dr, dc in D8_OFFSETS.values():
        nr, nc = r + dr, c + dc
        if 0 <= nr < dem.shape[0] and 0 <= nc < dem.shape[1] and (dem[r, c] - dem[nr, nc]) / np.hypot(dr, dc) > best:
            best, cell = (dem[r, c] - dem[nr, nc]) / np.hypot(dr, dc), (nr, nc)
    return cell


def test_a_line_down_a_steep_slope_isnt_cut_below_the_ground():
    # Ground falling 3 m a cell east, level across. The ground beside each cell, ahead of it, is as low as its next
    # cell, but its water falls more steeply straight on. The burn used to cut every cell 3 m, down to that ground
    dem = np.tile(200.0 - 3.0 * np.arange(20), (11, 1)).astype(np.float32)
    burned, _ = burn(dem, line_east_along_row(5, 20), np.zeros(dem.shape, bool), order_col=None, area_col=None)

    assert np.array_equal(burned[5, :-1], dem[5, :-1])  # its end on the DEM's edge is 0.5 m lower
    assert np.array_equal(np.delete(burned, 5, axis=0), np.delete(dem, 5, axis=0))
    assert all(steepest_way_down(burned, 5, c) == (5, c + 1) for c in range(19))


def test_a_line_over_a_cliff_isnt_cut_to_the_foot_of_the_cliff_beside_it():
    # A plateau at 300 m and a cliff: the line's cell below the lip is at 250 m, the ground either side of that cell
    # at 230 m, and the foot at 200 m. The burn used to cut the lip to 230 m, the lowest ground beside it; on
    # N39W109 it cut GEOGLOWS lines 50-73 m deep where they go over cliffs, and kept them low downstream
    dem = np.full((11, 20), 300.0, np.float32)
    dem[:, 10:] = 200.0
    dem[4:7, 10] = (230.0, 250.0, 230.0)
    burned, _ = burn(dem, line_east_along_row(5, 20), np.zeros(dem.shape, bool), order_col=None, area_col=None)

    assert np.array_equal(burned[5, :-1], dem[5, :-1])
    assert steepest_way_down(burned, 5, 9) == (5, 10) and steepest_way_down(burned, 5, 10) == (5, 11)


def test_a_line_is_still_lowered_where_the_ground_beside_it_would_take_its_water():
    # A line falling 0.5 m a cell, 1 m below the ground beside it, but for a cell beside its 10th cell 2 m lower
    # than that cell: the water in the line's 9th cell would fall onto it rather than along the line
    dem = np.tile(101.0 - 0.5 * np.arange(20), (11, 1)).astype(np.float32)
    dem[5] -= 1.0
    dem[4, 10] = dem[5, 10] - 2.0
    burned, _ = burn(dem, line_east_along_row(5, 20), np.zeros(dem.shape, bool), order_col=None, area_col=None)

    assert burned[5, 10] < dem[5, 10]
    assert all(steepest_way_down(burned, 5, c) in ((5, c + 1), None) for c in range(19))  # None: level


# ---------------------------------------------------------------------------------------------------------------
# Lakes, for the mappers other than FLDPLN, and dropping reaches
# ---------------------------------------------------------------------------------------------------------------

def network(rows):
    return gpd.GeoDataFrame({"LINKNO": [r[0] for r in rows], "DSLINKNO": [r[1] for r in rows]},
                            geometry=[LineString([(0, 0), (1, 1)])] * len(rows))


def test_lake_reaches_go_unless_they_carry_a_river_through_the_lake():
    #   1 -> 2 (in the lake) -> 3         2 carries the river through: kept
    #   4 -> 5 (in the lake), an outlet   5 leads nowhere outside the lake: dropped
    #   6 (in the lake) -> 7              6 has nothing above it outside the lake: dropped
    streams = network([(1, 2), (2, 3), (3, -1), (4, 5), (5, -1), (6, 7), (7, -1)])
    raster = np.array([[1, 2, 3, 4, 5, 6, 7]], np.int32)
    lakes = np.array([[False, True, False, False, True, True, False]])

    assert _reaches_stuck_in_lakes(streams, raster, lakes, "LINKNO", "DSLINKNO") == {5, 6}


def test_leaving_out_a_reach_frees_the_reaches_above_it():
    streams = network([(1, 2), (2, 3), (3, -1)])

    streams = _without_reaches(streams, {2}, "LINKNO", "DSLINKNO")

    assert list(streams.LINKNO) == [1, 3]
    assert streams.set_index("LINKNO").at[1, "DSLINKNO"] == -1


def left_out_workspace(tmp, fldpln=True, lakes=None, **configs):
    """
    Just what _reaches_left_out reads: a 10 x 10 DEM, the source streams (DEM_StrmShp, not written), and ``lakes``,
    a mask on the DEM's grid, as load_lake_array leaves it in lake_raster.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    write_raster(tmp / "dem.tif", np.full((10, 10), 100.0, np.float32), nodata=-9999.0)
    settings = dict(stream_id_field="LINKNO", downstream_id_field="DSLINKNO", parallel=False, overwrite=False,
                    mapper=SimpleNamespace(is_curve2flood_fldpln_mapper=lambda: fldpln), lakes=None, ocean=None,
                    burn_streams=True, area_km2_field=None, area_m2_field=None, StrmOrder_Field="strmOrder",
                    StrmOrder_Lower=None, StrmOrder_Upper=None, drop_multilinestrings=False)
    settings.update(configs)
    workspace = SimpleNamespace(configs=SimpleNamespace(**settings), assigned_dem=tmp / "dem.tif",
                                DEM_StrmShp=tmp / "streams.gpkg", lake_raster=tmp / "lakes.tif")
    if lakes is not None:
        workspace.configs.lakes = "lakes.gpkg"
        write_raster(workspace.lake_raster, lakes.astype(np.uint8), gdal.GDT_Byte)
    return workspace, Raster(workspace.assigned_dem)


def forked_headwater():
    """
    Reach 2 flows into 1 along row 5. 2 is a GEOGLOWS headwater whose two first-order branches were merged into it:
    a fork its line can't be drawn through in one piece.
    """
    stem = LineString([centre(5, c) for c in (5, 4, 3)])
    branches = [LineString([centre(r, 6) for r in (2, 3, 4)] + [centre(5, 5)]),
                LineString([centre(r, 6) for r in (8, 7, 6)] + [centre(5, 5)])]
    return gpd.GeoDataFrame({"LINKNO": [1, 2], "DSLINKNO": [-1, 1], "strmOrder": [2, 2]},
                            geometry=[LineString([centre(5, c) for c in (3, 2, 1, 0)]),
                                      MultiLineString([stem] + branches)], crs="EPSG:32615")


def test_a_forked_reach_is_left_out_with_drop_multilinestrings(workspace_tmp):
    workspace, dem = left_out_workspace(workspace_tmp, drop_multilinestrings=True)
    assert _reaches_left_out(workspace, forked_headwater(), dem) == {2}

    workspace.configs.drop_multilinestrings = False
    assert _reaches_left_out(workspace, forked_headwater(), dem) == set()


def test_the_reaches_left_out_are_decided_on_the_lines_before_relocation(workspace_tmp):
    # Relocation redraws the forked reach as one line. A rerun reads those lines, and still leaves it out.
    workspace, dem = left_out_workspace(workspace_tmp, drop_multilinestrings=True)
    source = forked_headwater()
    source.to_file(workspace.DEM_StrmShp)
    relocated = source.copy()
    relocated.loc[relocated.LINKNO == 2, "geometry"] = LineString([centre(2, 6), centre(5, 5), centre(5, 3)])

    assert _reaches_left_out(workspace, relocated, dem) == {2}


def test_other_mappers_leave_out_a_reach_ending_in_a_lake_before_the_burn(workspace_tmp):
    # 2 runs from 1 into the lake on the east and ends there: it leads nowhere outside the lake
    lakes = np.zeros((10, 10), bool)
    lakes[3:8, 7:] = True
    source = gpd.GeoDataFrame({"LINKNO": [1, 2], "DSLINKNO": [2, -1], "strmOrder": [1, 2]},
                              geometry=[LineString([centre(5, c) for c in range(5)]),
                                        LineString([centre(5, c) for c in range(4, 10)])], crs="EPSG:32615")

    workspace, dem = left_out_workspace(workspace_tmp / "other", fldpln=False, lakes=lakes)
    assert _reaches_left_out(workspace, source, dem) == {2}
    # FLDPLN cuts the network at the lake instead
    workspace, dem = left_out_workspace(workspace_tmp / "fldpln", fldpln=True, lakes=lakes)
    assert _reaches_left_out(workspace, source, dem) == set()


def estuary():
    """
    A 10 x 10 tile whose sea reaches up the river's channel: rows 3 to 7 from column 6 east. The river (1, 2) flows
    east along row 5 and into the sea; 3 starts there, in the sea, and ends on the bank north of it, drawn from its
    end up, as TDX-Hydro draws them; 4 runs on from there over the bank, and 5, a tributary, joins it.
    """
    ocean = np.zeros((10, 10), bool)
    ocean[3:8, 6:] = True
    source = gpd.GeoDataFrame(
        {"LINKNO": [1, 2, 3, 4, 5], "DSLINKNO": [2, 3, 4, -1, 4], "strmOrder": [3, 3, 3, 3, 1]},
        geometry=[LineString([centre(5, c) for c in range(5)]), LineString([centre(5, c) for c in range(4, 8)]),
                  LineString([centre(5, 7), centre(5, 8), centre(4, 9), centre(3, 9), centre(2, 9)][::-1]),
                  LineString([centre(r, 9) for r in (2, 1, 0)]), LineString([centre(0, 7), centre(1, 8), centre(2, 9)])],
        crs="EPSG:32615")
    return source, ocean


def test_the_reaches_below_where_their_river_enters_the_sea_are_in_it():
    # 2 starts on land and runs into the sea: it is the river's last reach. 3 starts in the sea. 4 starts on the bank,
    # but the river flowing into it is in the sea; 5, a tributary, starts on land.
    source, ocean = estuary()
    assert _reaches_in_the_sea(source, ocean, GT, "LINKNO", "DSLINKNO", "strmOrder") == {3, 4}


def test_a_line_ends_where_its_river_first_reaches_the_sea():
    # The sea crosses the tile at columns 5 and 6. Reach 2 runs east into it, out the other side and on over the land:
    # its river ends at the first shore. It is drawn from its end up, as TDX-Hydro draws them, and stays so.
    ocean = np.zeros((10, 10), bool)
    ocean[:, 5:7] = True
    source = gpd.GeoDataFrame({"LINKNO": [1, 2], "DSLINKNO": [2, -1]},
                              geometry=[LineString([centre(4, c) for c in range(3)]),
                                        LineString([centre(4, c) for c in range(2, 10)][::-1])], crs="EPSG:32615")

    cut = _cut_at_the_sea(source, ocean, GT, "LINKNO", "DSLINKNO").set_index("LINKNO").geometry
    assert cut[1].equals(source.geometry[0])
    xs = [x for x, _ in cut[2].coords]
    assert cut[2].coords[-1] == centre(4, 2)
    assert X0 + 5 * CELL <= xs[0] <= X0 + 5.5 * CELL and xs[0] == max(xs)


def test_reaches_in_the_sea_are_left_out_before_the_burn(workspace_tmp):
    source, ocean = estuary()
    ocean_path = workspace_tmp / "ocean.gpkg"
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(geometry=[box(X0 + 6 * CELL, Y0 - 8 * CELL, X0 + 10 * CELL, Y0 - 3 * CELL)],
                     crs="EPSG:32615").to_file(ocean_path)

    for fldpln in (True, False):
        workspace, dem = left_out_workspace(workspace_tmp / str(fldpln), fldpln=fldpln, ocean=str(ocean_path))
        assert _reaches_left_out(workspace, source, dem) == {3, 4}
    # Unburned, the ocean is ignored
    workspace.configs.burn_streams = False
    assert _reaches_left_out(workspace, source, dem) == set()


def test_other_mappers_leave_out_reaches_outside_the_stream_order_bounds(workspace_tmp):
    source = network([(1, 2), (2, -1)]).assign(strmOrder=[1, 2])

    workspace, dem = left_out_workspace(workspace_tmp / "other", fldpln=False, StrmOrder_Lower=2)
    assert _reaches_left_out(workspace, source, dem) == {1}
    workspace, dem = left_out_workspace(workspace_tmp / "fldpln", fldpln=True, StrmOrder_Lower=2)
    assert _reaches_left_out(workspace, source, dem) == set()


def test_a_forked_reach_is_cut_to_its_trunk_the_piece_joining_the_reach_below(workspace_tmp):
    _, dem = left_out_workspace(workspace_tmp)
    source = forked_headwater()

    trunks = _trunks(source, "LINKNO", "DSLINKNO", dem).set_index("LINKNO").geometry
    assert trunks[2].equals(LineString([centre(5, c) for c in (5, 4, 3)]))
    assert trunks[1].equals(source.geometry[0])


def test_with_no_reach_below_the_trunk_is_the_piece_with_the_lowest_end(workspace_tmp):
    # The ground rises east, so the trunk's west end is the lowest of the fork's ends
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    write_raster(workspace_tmp / "dem.tif", 100 + np.tile(np.arange(10, dtype=np.float32), (10, 1)), nodata=-9999.0)
    source = forked_headwater()
    source = source[source.LINKNO == 2].assign(DSLINKNO=-1)

    trunks = _trunks(source, "LINKNO", "DSLINKNO", Raster(workspace_tmp / "dem.tif"))
    assert trunks.geometry.iloc[0].equals(LineString([centre(5, c) for c in (5, 4, 3)]))


# ---------------------------------------------------------------------------------------------------------------
# The whole step
# ---------------------------------------------------------------------------------------------------------------

def synthetic_watershed(tmp, mapper="Curve2Flood-FLDPLNpy", with_lake=True):
    """
    An 80 x 80 tile of V-shaped valleys: a river flowing east along row 40 and off the east edge, and a tributary
    from the north joining it at column 40. A lake covers the river from column 60 to 68, and reach 14 crosses it,
    with river reaches above and below it. The source network draws its lines a cell off the valley floors, and
    from the outlet up, as TDX-Hydro does.
    """
    rows, cols = np.mgrid[0:80, 0:80]
    main = 10 + 0.2 * (79 - cols) + 2.0 * np.abs(rows - 40)
    trib = np.where(rows <= 40, 10 + 0.2 * 39 + 0.2 * (40 - rows) + 2.0 * np.abs(cols - 40), np.inf)
    dem = np.minimum(main, trib).astype(np.float32)
    dem_path = tmp / "dem.tif"
    write_raster(dem_path, dem, nodata=-9999.0)

    def line(cells):
        return LineString([centre(r + 0.4, c - 0.3) for r, c in cells][::-1])

    streams = gpd.GeoDataFrame(
        {"LINKNO": [11, 12, 13, 14, 15], "DSLINKNO": [13, 13, 14, 15, -1], "strmOrder": [2, 1, 2, 2, 2],
         "DSContArea": [2.0e6, 1.0e6, 3.5e6, 4.0e6, 4.2e6]},
        geometry=[line([(40, c) for c in range(41)]), line([(r, 40) for r in range(41)]),
                  line([(40, c) for c in range(40, 57)]), line([(40, c) for c in range(56, 73)]),
                  line([(40, c) for c in range(72, 80)])],
        crs="EPSG:32615")
    config = {
        "name": "synthetic", "dem_dir": str(tmp), "flowline": str(tmp / "streams.gpkg"), "output_dir": str(tmp / "out"),
        "mapper": mapper,
        "streamflow_source": "GEOGLOWS", "stream_id_field": "LINKNO", "downstream_id_field": "DSLINKNO",
        "StrmOrder_Field": "strmOrder", "area_m2_field": "DSContArea", "move_stream_network_to_thalweg": True,
        "burn_streams": True, "new_strm_threshold_km2": 0.05, "clean_dem": False, "make_fist_inputs": False,
        "use_parquet": True, "streams_as_parquet": False, "short_file_names": True, "compression": "DEFLATE",
    }
    if with_lake:
        lake_path = tmp / "lakes.gpkg"
        x0, y1 = centre(34, 60)
        x1, y0 = centre(46, 68)
        gpd.GeoDataFrame({"Lake_id": [1]}, geometry=[box(x0, y0, x1, y1)], crs="EPSG:32615").to_file(lake_path)
        config["lakes"] = str(lake_path)
    workspace = Workspace(NencartaConfig(config), dem_path)
    workspace.DEM_StrmShp.parent.mkdir(parents=True, exist_ok=True)
    streams.to_file(workspace.DEM_StrmShp)
    water = np.zeros(dem.shape, np.uint8)
    water[40, :] = 1
    water[:41, 40] = 1
    workspace.bathy_water_mask.parent.mkdir(parents=True, exist_ok=True)
    write_raster(workspace.bathy_water_mask, water, gdal.GDT_Byte)
    return workspace


def read_d8_paths(workspace):
    flowdir = Raster(workspace.flowdir).read_array()
    raster = Raster(workspace.new_stream_raster).read_array()
    streams = gpd.read_file(workspace.new_StrmShp_matched)
    return flowdir, raster, streams


def test_burn_and_move_on_a_synthetic_watershed(workspace_tmp):
    workspace = synthetic_watershed(workspace_tmp)
    burn_streams_and_move_streams(workspace)

    flowdir, raster, streams = read_d8_paths(workspace)
    lakes = Raster(workspace.lake_raster).read_array().astype(bool)

    # FLDPLN doesn't map lakes: nothing runs inside one, the river above the lake ends at its shore, and the river
    # below it starts at the other shore
    assert {11, 12, 13, 15} <= set(streams.LINKNO)
    assert lakes.any() and not (raster.astype(bool) & lakes).any()
    ds = streams.set_index("LINKNO")["DSLINKNO"].to_dict()
    assert ds[11] == 13 and ds[12] == 13
    lake_cols = np.nonzero(lakes[40])[0]
    assert np.nonzero(raster == 13)[1].max() < lake_cols.min()
    assert np.nonzero(raster == 15)[1].min() > lake_cols.max()

    # Every reach is one D8 path, and its cells are the raster's
    for _, row in streams.iterrows():
        cells = [(int((Y0 - y) // CELL), int((x - X0) // CELL)) for x, y in row.geometry.coords]
        for a, b in zip(cells[:-1], cells[1:]):
            d_row, d_col = D8_OFFSETS[int(flowdir[a])]
            assert (a[0] + d_row, a[1] + d_col) == b
        assert all(raster[cell] == row.LINKNO for cell in cells)
    # Both rivers run down their valley floors, not where the source drew them
    assert set(np.unique(np.nonzero(raster == 11)[0])) == {40}
    assert set(np.unique(np.nonzero(raster == 12)[1])) == {40}

    table = pd.read_parquet(workspace.stream_info_file)
    assert set(table.stream_id) == set(streams.LINKNO)


def reach_in_pieces(tmp):
    """
    The synthetic watershed with the tributary (12) drawn in two pieces that don't join: the longer one 4 cells up
    its valley's east side, the other on the ridge to the west. Relocation would redraw it down its valley floor.
    """
    workspace = synthetic_watershed(tmp, with_lake=False)
    workspace.configs.drop_multilinestrings = True
    source = gpd.read_file(workspace.DEM_StrmShp)
    trunk = LineString(([centre(r, 44) for r in range(37)] + [centre(39, 41)])[::-1])
    stub = LineString([centre(r, 30) for r in range(5, 10)][::-1])
    source.loc[source.LINKNO == 12, "geometry"] = MultiLineString([trunk, stub])
    source.to_file(workspace.DEM_StrmShp)
    return workspace, source


def test_a_reach_in_pieces_is_left_out_before_it_is_burned(workspace_tmp):
    workspace, source = reach_in_pieces(workspace_tmp)
    burn_streams_and_move_streams(workspace)

    # Neither its lines nor the valley floor relocation would have moved it onto are carved into the DEM
    burned_by = Raster(workspace.assigned_dem).read_array() - Raster(workspace.fixed_dem).read_array()
    assert not burned_by[:30, 44].any() and not burned_by[5:10, 30].any() and not burned_by[:30, 40].any()
    assert not workspace.relocated_StrmShp.exists() or \
        12 not in set(gpd.read_file(workspace.relocated_StrmShp).LINKNO)
    _, raster, streams = read_d8_paths(workspace)
    assert not (raster == 12).any()
    # Every other reach is there
    assert set(streams.LINKNO) == set(source.LINKNO) - {12}


def test_kept_in_pieces_a_reach_is_burned_along_its_trunk_alone(workspace_tmp):
    # The tributary (12) as GEOGLOWS draws a headwater: its trunk up the valley floor from the river to row 20, where
    # it forks into a long branch on up the valley and a short one up the valley's east side
    workspace = synthetic_watershed(workspace_tmp, with_lake=False)
    source = gpd.read_file(workspace.DEM_StrmShp)

    def line(cells):
        return LineString([centre(r + 0.4, c - 0.3) for r, c in cells][::-1])

    short = [(20 - k, 40 + k) for k in range(7)]
    source.loc[source.LINKNO == 12, "geometry"] = MultiLineString(
        [line([(r, 40) for r in range(20, 41)]), line([(r, 40) for r in range(21)]), line(short)])
    source.to_file(workspace.DEM_StrmShp)
    burn_streams_and_move_streams(workspace)

    burned_by = Raster(workspace.assigned_dem).read_array() - Raster(workspace.fixed_dem).read_array()
    assert burned_by[22:39, 40].min() > 0
    assert (burned_by[[r for r, _ in short[1:]], [c for _, c in short[1:]]] <= 0).all()
    assert (burned_by[:19, 40] <= 0).all()
    # Mapped up its trunk and long branch
    _, raster, streams = read_d8_paths(workspace)
    assert 12 in set(streams.LINKNO)
    assert np.nonzero(raster == 12)[0].min() < 10


def test_moving_again_on_an_older_burn_still_leaves_out_a_reach_in_pieces(workspace_tmp):
    # An older run burned and relocated every reach, and left 12 out only after the burn. Moving the streams again
    # on its DEM reads its relocated lines, 12's redrawn as one, and leaves 12 out all the same.
    workspace, source = reach_in_pieces(workspace_tmp)
    burn_streams_and_move_streams(workspace)
    older = source.copy()
    older.loc[older.LINKNO == 12, "geometry"] = LineString([centre(r, 40) for r in range(41)][::-1])
    older.to_file(workspace.relocated_StrmShp)
    workspace.new_StrmShp_matched.unlink()
    burn_streams_and_move_streams(workspace)

    _, raster, streams = read_d8_paths(workspace)
    assert not (raster == 12).any()
    assert set(streams.LINKNO) == set(source.LINKNO) - {12}


def test_ground_at_0_m_is_burned_and_moved_onto_like_any_other(workspace_tmp):
    # The river's lower half, and the valley floor around it, are at 0 m: lowland, not the sea (the sea goes in the
    # lakes). The ground off the channel keeps its height in the burned DEM, and the outlet reach runs across it to
    # the edge
    workspace = synthetic_watershed(workspace_tmp, with_lake=False)
    dem = np.maximum(Raster(workspace.assigned_dem).read_array() - 20, 0).astype(np.float32)
    write_raster(workspace.assigned_dem, dem, nodata=-9999.0)
    burn_streams_and_move_streams(workspace)

    fixed = Raster(workspace.fixed_dem).read_array()
    assert not (fixed == -9999.0).any()
    rows, cols = np.mgrid[0:80, 0:80]
    off_the_channel = (dem == 0) & (np.abs(rows - 40) >= 3) & (cols > 50)  # away from the river and the tributary
    assert off_the_channel.sum() > 20 and (fixed[off_the_channel] == 0).all()
    _, raster, streams = read_d8_paths(workspace)
    assert set(streams.LINKNO) == {11, 12, 13, 14, 15}
    assert np.nonzero(raster == 15)[1].max() == 79


@pytest.mark.parametrize("mapper", ["Curve2Flood-FLDPLNpy", "Curve2Flood-Kernel Weighted"])
def test_the_ocean_is_off_the_burned_dem_and_the_river_ends_at_its_shore(workspace_tmp, mapper):
    # The sea covers column 66 on: reach 14 runs out into it, and 15 is wholly in it
    workspace = synthetic_watershed(workspace_tmp, mapper=mapper, with_lake=False)
    ocean_path = workspace_tmp / "ocean.gpkg"
    gpd.GeoDataFrame({"name": ["sea"]}, geometry=[box(X0 + 66 * CELL, Y0 - 80 * CELL, X0 + 90 * CELL, Y0)],
                     crs="EPSG:32615").to_file(ocean_path)
    workspace.configs.ocean = str(ocean_path)
    burn_streams_and_move_streams(workspace)

    ocean = np.zeros((80, 80), bool)
    ocean[:, 66:] = True
    fixed = Raster(workspace.fixed_dem).read_array()
    assert ((fixed == -9999.0) == ocean).all()
    flowdir, raster, streams = read_d8_paths(workspace)
    assert (flowdir[ocean] == FLOWDIR_NODATA).all() and not (raster[ocean]).any()
    # Unlike a sea in the lakes, the outlet reach is kept with every mapper, ending at the shore
    assert streams.set_index("LINKNO")["DSLINKNO"].to_dict() == {11: 13, 12: 13, 13: 14, 14: -1}
    assert np.nonzero(raster == 14)[1].max() == 65
    assert not Raster(workspace.bathy_water_mask).read_array()[ocean].any()

    # Unburned, the DEM is mapped as it is
    workspace.configs.burn_streams = False
    assert load_ocean_array(workspace, Raster(workspace.assigned_dem)) is None


def test_other_mappers_keep_a_lake_reach_that_carries_the_river(workspace_tmp):
    workspace = synthetic_watershed(workspace_tmp, mapper="Curve2Flood-Kernel Weighted")
    burn_streams_and_move_streams(workspace)

    _, raster, streams = read_d8_paths(workspace)
    # 14 crosses the lake with river above and below it, so the old rule keeps it, whole
    assert set(streams.LINKNO) == {11, 12, 13, 14, 15}
    assert streams.set_index("LINKNO")["DSLINKNO"].to_dict() == {11: 13, 12: 13, 13: 14, 14: 15, 15: -1}
    lakes = Raster(workspace.lake_raster).read_array().astype(bool)
    assert (raster[lakes] == 14).any()


def misplaced_watershed(tmp, relocate=True, burn=True):
    """
    A 60 x 120 valley whose river flows east along row 30 and off the east edge, each row beside it 1 m higher.
    The source draws reach 22 eight rows north of the river, on the valley side 8 m above it, between 21 and 23,
    which are on it.
    """
    rows, cols = np.mgrid[0:60, 0:120]
    dem = (20 + 0.05 * (119 - cols) + 1.0 * np.abs(rows - 30)).astype(np.float32)
    dem_path = tmp / "dem.tif"
    write_raster(dem_path, dem, nodata=-9999.0)

    def line(cells):
        return LineString([centre(r, c) for r, c in cells][::-1])

    column_m2 = 60 * CELL * CELL  # what each column of the valley drains
    streams = gpd.GeoDataFrame(
        {"LINKNO": [21, 22, 23], "DSLINKNO": [22, 23, -1], "strmOrder": [3, 3, 3],
         "DSContArea": [41 * column_m2, 81 * column_m2, 120 * column_m2]},
        geometry=[line([(30, 0), (30, 40)]), line([(30, 40), (22, 46), (22, 74), (30, 80)]),
                  line([(30, 80), (30, 119)])],
        crs="EPSG:32615")
    config = {
        "name": "misplaced", "dem_dir": str(tmp), "flowline": str(tmp / "streams.gpkg"), "output_dir": str(tmp / "out"),
        "mapper": "Curve2Flood-FLDPLNpy", "streamflow_source": "GEOGLOWS", "stream_id_field": "LINKNO",
        "downstream_id_field": "DSLINKNO", "StrmOrder_Field": "strmOrder", "area_m2_field": "DSContArea",
        "move_stream_network_to_thalweg": True, "burn_streams": burn, "relocate_misplaced_streams": relocate,
        "new_strm_threshold_km2": 0.05, "clean_dem": False, "make_fist_inputs": False, "use_parquet": True,
        "streams_as_parquet": False, "short_file_names": True, "compression": "DEFLATE",
    }
    workspace = Workspace(NencartaConfig(config), dem_path)
    workspace.DEM_StrmShp.parent.mkdir(parents=True, exist_ok=True)
    streams.to_file(workspace.DEM_StrmShp)
    # As make_stream_raster and make_water_mask leave them: the source lines rasterized with their ids, and the
    # water mask made from them
    stream_raster = np.zeros(dem.shape, np.int32)
    stream_raster[30, :41] = 21
    stream_raster[22, 46:75] = 22
    for k in range(1, 8):  # its legs down to the river, which keep it joined to the river's cells
        stream_raster[30 - k, 40 + k] = stream_raster[30 - k, 80 - k] = 22
    stream_raster[30, 80:] = 23
    write_raster(workspace.STRM_File_Clean, stream_raster, gdal.GDT_Int32)
    workspace.bathy_water_mask.parent.mkdir(parents=True, exist_ok=True)
    write_raster(workspace.bathy_water_mask, (stream_raster > 0).astype(np.uint8), gdal.GDT_Byte)
    return workspace


@pytest.mark.parametrize("relocate", [True, False])
def test_a_reach_drawn_off_its_river_is_burned_where_the_dem_has_the_river(relocate, workspace_tmp):
    workspace = misplaced_watershed(workspace_tmp, relocate)
    burn_streams_and_move_streams(workspace)

    _, raster, streams = read_d8_paths(workspace)
    rows_of_22 = set(np.unique(np.nonzero(raster == 22)[0]))
    if relocate:
        assert set(streams.LINKNO) == {21, 22, 23}
        assert rows_of_22 == {30}
        assert workspace.relocated_StrmShp.exists()
        # 22's old line is gone from the water mask ARC carves bathymetry into, and the river is in it
        water_mask = Raster(workspace.bathy_water_mask).read_array()
        assert not water_mask[22, 46:75].any()
        assert water_mask[30, 41:80].all()
    else:
        # Burned where the source draws it, up on the valley side, but the moved network still has every reach: the
        # conflation's wider search finds 22 on the river. So nothing runs along the burned line, and the DEM is
        # put back there (see test_a_line_burned_where_no_moved_stream_runs_is_put_back)
        assert not workspace.relocated_StrmShp.exists()
        assert set(streams.LINKNO) == {21, 22, 23}
        assert rows_of_22 == {30}


def test_a_line_burned_where_no_moved_stream_runs_is_put_back(workspace_tmp, caplog):
    # Reach 22, burned where the source draws it on the valley side and moved onto the river, leaves a trench along
    # row 22 that no stream runs in. The DEM is put back there, except within a cell of the river, and the valley
    # side drains to the river again, not along the trench
    workspace = misplaced_watershed(workspace_tmp, relocate=False)
    with caplog.at_level("INFO", logger="nencarta"):
        burn_streams_and_move_streams(workspace)

    assert "Restoring the unburned DEM" in caplog.text
    unburned = Raster(workspace.assigned_dem).read_array()
    fixed = Raster(workspace.fixed_dem).read_array()
    assert (fixed[22, 46:75] == unburned[22, 46:75]).all()
    assert (fixed[23:29, 41:80] == unburned[23:29, 41:80]).all()  # its legs down to the river, but for the last cell
    flowdir, raster, _ = read_d8_paths(workspace)
    assert all(D8_OFFSETS[int(flowdir[22, c])][0] == 1 for c in range(50, 71))
    filled = Raster(workspace.filled_dem).read_array()
    assert filled[22, 46:75] == pytest.approx(unburned[22, 46:75], abs=1e-3)
    # The river's cells are still the moved network's, and the stream info table walks them
    assert set(np.unique(np.nonzero(raster == 22)[0])) == {30}
    table = pd.read_parquet(workspace.stream_info_file)
    assert set(table.stream_id) == {21, 22, 23}


def test_a_burn_that_drains_the_network_s_water_stays(tmp_path):
    # A plain falling south, with a reservoir dug 3 m into it (the water mask) and a river through it, whose outlet is
    # a notch burned through its rim to the river below, which starts 5 cells below it, and a trench burned down the
    # plain far from both. Put back, the notch would dam the reservoir, which the filled DEM would then hold 3 m higher
    # over the river in it: it stays burned. The trench drains nothing of the network's: it is put back.
    rows, cols = np.mgrid[0:40, 0:40]
    unburned = (100.0 - 0.5 * rows).astype(np.float32)
    basin = (rows >= 5) & (rows <= 12) & (cols >= 5) & (cols <= 15)
    unburned[basin] -= 3.0
    fixed = unburned.copy()
    floor = float(unburned[12, 10])
    fixed[13:18, 10] = [floor - 0.1 * k for k in range(1, 6)]  # the notch, falling from the reservoir to the river
    fixed[5:, 30] -= 3.0  # the trench, down to the plain's south edge
    streams = np.zeros(fixed.shape, np.int32)
    streams[5:13, 10] = 6
    streams[18:, 10] = 7
    water = basin.copy()
    water[18:, 10] = True
    paths = SimpleNamespace(fixed_dem=tmp_path / "fixed.tif", assigned_dem=tmp_path / "dem.tif",
                            filled_dem=tmp_path / "filled.tif", flowdir=tmp_path / "flowdir.tif",
                            lake_raster=tmp_path / "lakes.tif",
                            configs=SimpleNamespace(lakes=None, compression="DEFLATE", overwrite=False))
    write_raster(paths.assigned_dem, unburned, nodata=-9999.0)
    write_raster(paths.fixed_dem, fixed, nodata=-9999.0)
    filled_before, flowdir_before = fill_and_flow_directions(fixed.astype(np.float64), np.ones(fixed.shape, bool), GT,
                                                             utm_wkt())
    write_raster(paths.filled_dem, filled_before, gdal.GDT_Float64, nodata=-9999.0)
    write_raster(paths.flowdir, flowdir_before, gdal.GDT_Int16, nodata=FLOWDIR_NODATA)

    filled_after, flowdir_after = restore_unfollowed_burns(paths, streams, water, flowdir=flowdir_before)

    after = Raster(paths.fixed_dem).read_array()
    assert (after[5:, 30] == unburned[5:, 30]).all()
    assert (after[13:18, 10] == fixed[13:18, 10]).all()
    assert filled_after[basin] == pytest.approx(filled_before[basin], abs=1e-6)
    assert (flowdir_after[streams > 0] == flowdir_before[streams > 0]).all()
    assert Raster(paths.filled_dem).read_array()[basin] == pytest.approx(filled_before[basin], abs=1e-6)


def write_land_cover(workspace, water):
    land = np.where(water, workspace.configs.land_watervalue, 10).astype(np.uint8)
    workspace.LAND_File.parent.mkdir(parents=True, exist_ok=True)
    write_raster(workspace.LAND_File, land, gdal.GDT_Byte)


def test_land_covers_water_well_above_the_river_beside_it_isnt_the_river():
    # A braided river's bed: the river's water along rows 8-12 at 100 m, and a bar 3 m higher with a tributary's line
    # running across it along row 20. Between them the bar is nearer the tributary than the river, and as high as the
    # tributary's line, but 3 m above the river, the river's level there. Far from the river (row 80), another
    # tributary's own water is near its level, and its banks aren't.
    dem = np.full((100, 60), 103.0)
    dem[8:13] = 100.0
    dem[60:] = 120.0
    dem[78:83] = 112.5
    dem[80] = 112.0
    streams = np.zeros(dem.shape, bool)
    streams[10, :] = streams[20, 20:40] = streams[80, :] = True
    near = near_the_rivers_level(dem, streams)
    assert near[8:13].all()
    assert not near[14:18, 20:40].any()
    assert near[78:83].all() and not near[60:77].any()


def test_the_water_beside_a_sloping_river_is_at_its_level():
    # A river falling 0.1 m a cell along row 10, its water six cells to either side at its level, and banks 5 m up.
    # Within twice as far as the nearest stream cell the river runs on down a metre and more: beside the river, the
    # water is at the level of the river beside it, not of the river farther down.
    dem = np.tile(50.0 - 0.1 * np.arange(200), (21, 1))
    dem[:4] += 5.0
    dem[17:] += 5.0
    streams = np.zeros(dem.shape, np.int32)
    streams[10, :] = 7
    near = near_the_rivers_level(dem, streams)
    assert near[4:17].all()
    assert not near[:4].any() and not near[17:].any()


def test_the_water_mask_has_the_water_the_dem_flattens_beside_the_river(tmp_path):
    # A DEM that flattens its water, here to no whole half metre, as USGS 3DEP does. Where the imagery saw no water
    # there (the river has moved since), it is still the channel this DEM has. A flat pond away from the river,
    # touching none of it, is not.
    rows, cols = np.mgrid[0:60, 0:80]
    dem = 31.123 + 0.0137 * rows + 0.0171 * cols  # nowhere flat
    dem[18:23, :] = 30.137
    dem[40:45, 30:41] = 30.637
    streams = np.zeros(dem.shape, np.int32)
    streams[20, :] = 5

    class Dem:
        nodata_value = -9999.0
        geotransform = GT
        projection = utm_wkt()

        def read_array(self):
            return dem

    def mask_with(channel):
        configs = SimpleNamespace(land_watervalue=80, use_dem_derived_channel_mask=channel, dem_flattens_rivers=None,
                                  ocean=None, lakes=None)
        return water_mask(SimpleNamespace(configs=configs, LAND_File=tmp_path / "no_land_cover.tif"), Dem(), streams,
                          {5: 100.0})

    with_channel = mask_with(True)
    assert with_channel[18:23].all()
    assert not with_channel[40:45, 30:41].any()
    assert np.array_equal(mask_with(False), streams > 0)


def test_a_reservoirs_water_by_its_dam_is_in_the_water_mask(tmp_path):
    # A reservoir at 100 m (rows 20-39, cols 10-49), dammed at cols 50-51, which FLDPLN's network is cut at: no stream
    # runs in it. A tributary (1) comes in from the top, and the river (2) leaves at the dam's foot, 5 m below the
    # reservoir. The river below the dam is the stream nearest the water by the dam, and the reservoir is well above
    # it: but that water is the lake's, at its own level.
    dem = np.full((60, 80), 110.0)
    reservoir = np.zeros(dem.shape, bool)
    reservoir[20:40, 10:50] = True
    dem[reservoir] = 100.0
    dem[25:35, 52:] = 95.0
    streams = np.zeros(dem.shape, np.int32)
    streams[:20, 30] = 1
    dem[:20, 30] = 100.5 + 0.05 * np.arange(19, -1, -1)
    streams[30, 52:] = 2
    write_raster(tmp_path / "land.tif", np.where(reservoir, 80, 10).astype(np.uint8), gdal.GDT_Byte)
    write_raster(tmp_path / "lakes.tif", reservoir.astype(np.uint8), gdal.GDT_Byte)

    class Dem:
        nodata_value = -9999.0
        geotransform = GT
        projection = utm_wkt()

        def read_array(self):
            return dem

    def mask_with(lakes):
        configs = SimpleNamespace(land_watervalue=80, use_dem_derived_channel_mask=False, ocean=None, lakes=lakes,
                                  overwrite=False)
        workspace = SimpleNamespace(configs=configs, LAND_File=tmp_path / "land.tif",
                                    lake_raster=tmp_path / "lakes.tif")
        return water_mask(workspace, Dem(), streams, {1: 10.0, 2: 100.0})

    mask = mask_with("lakes.gpkg")
    assert mask[reservoir].all()
    assert np.array_equal(mask, reservoir | (streams > 0))
    # Without the lakes, the water by the dam is judged by the river below it
    assert not mask_with(None)[20:40, 45:50].any()


def test_the_water_mask_is_the_moved_streams_and_the_land_covers_water(workspace_tmp):
    # The mask the step starts from also has a line no reach is moved onto, a spur off the river (as a line the
    # water doesn't take leaves): ARC would carve bathymetry into it, and it would flood. Water along the river, at
    # its level, stays; the land cover's water running on up the valley side from it, metres above the river, is
    # ground the DEM shows dry, which ARC would carve down to the river's bed. (Along the whole middle reach, so the
    # river isn't one the mask has mostly 1 cell wide: see one_cell_wide_where_mostly.)
    workspace = misplaced_watershed(workspace_tmp)
    dem = Raster(workspace.assigned_dem).read_array()
    dem[31:34, 41:80] = dem[30, 41:80]
    write_raster(workspace.assigned_dem, dem, nodata=-9999.0)
    mask = Raster(workspace.bathy_water_mask).read_array()
    pond = np.zeros(mask.shape, bool)
    pond[31:34, 41:80] = True
    up_the_side = np.zeros(mask.shape, bool)
    up_the_side[34:38, 41:80] = True
    mask[31:46, 100] = 1
    mask[pond | up_the_side] = 1
    write_raster(workspace.bathy_water_mask, mask, gdal.GDT_Byte)
    write_land_cover(workspace, pond | up_the_side)
    burn_streams_and_move_streams(workspace)

    _, raster, _ = read_d8_paths(workspace)
    water_mask = Raster(workspace.bathy_water_mask).read_array().astype(bool)
    assert not water_mask[31:46, 100].any()
    assert water_mask[pond].all()
    assert not water_mask[up_the_side].any()
    assert np.array_equal(water_mask & ~pond, raster > 0)


def test_a_reach_the_water_mask_has_mostly_one_cell_wide_is_one_cell_wide_all_along():
    # Reach 1 runs along row 10, a cell wide but for a pond beside it (rows 11-14, cols 20-27) and a lake (rows 3-9,
    # cols 60-70). Reach 2 runs along row 30 in its river's water (rows 27-33); reach 3 comes down column 50 into it,
    # a cell wide but for its last cells, in reach 2's water
    streams = np.zeros((40, 80), np.int32)
    streams[10, :] = 1
    streams[30, :] = 2
    streams[15:30, 50] = 3
    pond = np.zeros(streams.shape, bool)
    pond[11:15, 20:28] = True
    lake = np.zeros(streams.shape, bool)
    lake[3:10, 60:71] = True
    river = np.zeros(streams.shape, bool)
    river[27:34] = True
    mask = (streams > 0) | pond | lake | river

    out = one_cell_wide_where_mostly(mask, streams, lake)
    assert np.array_equal(out, mask & ~pond)  # the river's water around reach 3's mouth stays
    assert np.array_equal(one_cell_wide_where_mostly(mask, streams), mask & ~pond & ~lake)
    # Mostly beside water, reach 1 keeps its pond
    wide = mask.copy()
    wide[11, :] = True
    assert np.array_equal(one_cell_wide_where_mostly(wide, streams), wide)


def test_the_water_mask_drops_the_land_covers_water_beside_a_river_a_cell_wide_but_in_lakes(tmp_path):
    # A river a cell wide along row 30 of a valley, each row beside it 0.5 m higher; the land cover has a pond by it
    # at its level (rows 31-33, cols 40-49). The river is a cell wide, so the pond isn't in the mask; in a lake, it is
    rows, cols = np.mgrid[0:60, 0:80]
    dem = 50 + 0.05 * (79 - cols) + 0.5 * np.abs(rows - 30) + 0.013
    pond = np.zeros(dem.shape, bool)
    pond[31:34, 40:50] = True
    dem[pond] = (50 + 0.05 * (79 - cols) + 0.013)[pond]
    streams = np.zeros(dem.shape, np.int32)
    streams[30, :] = 1
    write_raster(tmp_path / "land.tif", np.where(pond, 80, 10).astype(np.uint8), gdal.GDT_Byte)
    write_raster(tmp_path / "lakes.tif", pond.astype(np.uint8), gdal.GDT_Byte)

    class Dem:
        nodata_value = -9999.0
        geotransform = GT
        projection = utm_wkt()

        def read_array(self):
            return dem

    def mask_with(lakes):
        configs = SimpleNamespace(land_watervalue=80, use_dem_derived_channel_mask=False, ocean=None, lakes=lakes,
                                  overwrite=False)
        workspace = SimpleNamespace(configs=configs, LAND_File=tmp_path / "land.tif",
                                    lake_raster=tmp_path / "lakes.tif")
        return water_mask(workspace, Dem(), streams, {1: 10.0})

    assert np.array_equal(mask_with(None), streams > 0)
    assert np.array_equal(mask_with("lakes.gpkg"), (streams > 0) | pond)


@pytest.mark.parametrize("overwrite", [True, False])
def test_the_water_mask_starts_from_the_moved_streams_unless_they_are_about_to_be_moved_again(overwrite,
                                                                                             workspace_tmp):
    workspace = misplaced_watershed(workspace_tmp)
    workspace.bathy_water_mask.unlink()
    write_land_cover(workspace, np.zeros((60, 120), bool))
    moved = np.zeros((60, 120), np.int32)
    moved[26, :] = 22  # where an earlier run moved the streams
    write_raster(workspace.new_stream_raster, moved, gdal.GDT_Int32)
    workspace.configs.overwrite = overwrite
    make_water_mask(workspace)

    expected = moved if not overwrite else Raster(workspace.STRM_File_Clean).read_array()
    assert np.array_equal(Raster(workspace.bathy_water_mask).read_array() > 0, expected > 0)


def test_moving_again_on_the_burned_dem_follows_the_relocated_streams(workspace_tmp):
    workspace = misplaced_watershed(workspace_tmp)
    burn_streams_and_move_streams(workspace)
    burned = Raster(workspace.fixed_dem).read_array()
    workspace.new_StrmShp_matched.unlink()
    workspace.new_stream_raster.unlink()

    burn_streams_and_move_streams(workspace)
    assert np.array_equal(Raster(workspace.fixed_dem).read_array(), burned)
    _, raster, _ = read_d8_paths(workspace)
    assert set(np.unique(np.nonzero(raster == 22)[0])) == {30}


def test_without_burning_a_reach_drawn_off_its_river_is_moved_onto_it(workspace_tmp):
    workspace = misplaced_watershed(workspace_tmp, burn=False)
    burn_streams_and_move_streams(workspace)

    _, raster, streams = read_d8_paths(workspace)
    assert set(streams.LINKNO) == {21, 22, 23}
    assert set(np.unique(np.nonzero(raster == 22)[0])) == {30}
    assert not workspace.fixed_dem.exists()


def follow(flowdir, row, col, steps):
    """The cells of the D8 path from (row, col)."""
    path = [(row, col)]
    for _ in range(steps):
        code = flowdir[path[-1]]
        if code not in D8_OFFSETS:
            break
        dr, dc = D8_OFFSETS[code]
        path.append((path[-1][0] + dr, path[-1][1] + dc))
    return path


def test_flow_across_a_flat_runs_down_its_middle():
    # A channel FABDEM leveled at 100 m, rows 2-8, falling off the east edge: away from its banks, the flow keeps to
    # its middle row instead of running straight along an edge
    dem = np.full((11, 40), 105.0)
    dem[2:9, 1:] = 100.0
    filled_dem, flowdir = fill_and_flow_directions(dem, np.ones(dem.shape, bool), GT, utm_wkt())
    assert np.array_equal(filled_dem, dem)
    path = follow(flowdir, 2, 1, 60)
    assert path[-1][1] == 39  # off the east edge
    assert {r for r, c in path if 6 <= c <= 33} == {5}


def test_a_flat_running_off_the_dem_drains_off_it_without_pits():
    # Flat everywhere but a ridge, with a nodata hole: every cell away from the edge and the hole drains somewhere
    dem = np.full((30, 30), 50.0, np.float32)
    dem[10, 5:25] = 60.0
    valid = np.ones(dem.shape, bool)
    valid[18:22, 12:16] = False
    filled_dem, flowdir = fill_and_flow_directions(dem, valid, GT, utm_wkt())
    assert (flowdir[~valid] == FLOWDIR_NODATA).all() and np.isnan(filled_dem[~valid]).all()
    inner = np.zeros(dem.shape, bool)
    inner[1:-1, 1:-1] = True
    inner[17:23, 11:17] = False
    assert set(np.unique(flowdir[valid])) <= set(D8_OFFSETS) | {0}
    assert (flowdir[inner & valid] != 0).all()

def test_depressions_are_filled_as_richdem_fills_them():
    # Each cell to its spill level, which is unique, so RichDEM's Priority-Flood gives the same: on ties, plateaus, pits
    # in pits and nodata holes, inside the ring lower than anything fill_depressions pads the DEM with. RichDEM isn't a
    # dependency: checked against it where it is installed.
    rd = pytest.importorskip("richdem", reason="RichDEM, to compare against, isn't installed")

    rng = np.random.default_rng(6)
    for trial in range(60):
        rows, cols = int(rng.integers(1, 40)), int(rng.integers(1, 40))
        dem = 100.0 + (rng.integers(0, 4, (rows, cols)) if trial % 2 else rng.random((rows, cols)) * 5)
        valid = rng.random((rows, cols)) > (0.0 if trial % 3 == 0 else 0.1)
        if not valid.any():
            continue
        low = dem[valid].min() - 10.0
        raise_by = 1.0 - np.floor(low)
        padded = np.full((rows + 2, cols + 2), low)
        padded[1:-1, 1:-1] = np.where(valid, dem, low)
        padded = rd.rdarray(padded + raise_by, no_data=-np.finfo(np.float64).max)
        rd.FillDepressions(padded, in_place=True)
        expected = np.where(valid, np.asarray(padded)[1:-1, 1:-1] - raise_by, np.nan)
        assert np.array_equal(fill_depressions(dem, valid), expected, equal_nan=True)
        if trial % 2:  # as SRTM and AW3D30 store them, whole metres as integers
            assert np.array_equal(fill_depressions(dem.astype(np.int16), valid), expected, equal_nan=True)


def test_a_dem_filled_but_where_lowered_fills_as_a_whole():
    # The burned DEM's routing copy is filled but for its lines, run down the fill: filled from those cells and the
    # cells off the DEM alone (see routing_dem), it gets the filled DEM and flow directions filling it whole would
    rng = np.random.default_rng(7)
    for trial in range(40):
        rows, cols = int(rng.integers(3, 45)), int(rng.integers(3, 45))
        dem = 100.0 + rng.random((rows, cols)) * 8
        valid = rng.random((rows, cols)) > (0.0 if trial % 3 == 0 else 0.15)
        routing = fill_depressions(dem, valid)
        lowered = np.zeros(dem.shape, bool)
        for _ in range(3):
            r, c, level = int(rng.integers(rows)), int(rng.integers(cols)), np.inf
            for _ in range(int(rng.integers(2, 40))):
                if valid[r, c]:
                    level = min(routing[r, c], level - 1e-6) - (2.0 * rng.random() if rng.random() < 0.3 else 0.0)
                    lowered[r, c] |= level < routing[r, c]
                    routing[r, c] = min(routing[r, c], level)
                r = min(max(r + int(rng.integers(-1, 2)), 0), rows - 1)
                c = min(max(c + int(rng.integers(-1, 2)), 0), cols - 1)
        routing[~valid] = -9999.0
        whole = fill_and_flow_directions(routing, valid, GT, utm_wkt())
        partly = fill_and_flow_directions(routing, valid, GT, utm_wkt(), np.flatnonzero(lowered))
        assert np.array_equal(whole[0], partly[0], equal_nan=True)
        assert np.array_equal(whole[1], partly[1])


@pytest.mark.parametrize("level", [0.0, 7.0, -3.0])
def test_a_flat_at_any_level_drains_even_at_0_m(level):
    # A lagoon FABDEM leveled at 0 m drains like any other flat: the gradient given a flat at 0 m was too small to be a
    # slope, so none of it had a flow direction. The filled DEM is the DEM, at its own levels.
    dem = np.full((9, 30), level + 3.0, np.float32)
    dem[1:8, :] = level
    filled_dem, flowdir = fill_and_flow_directions(dem, np.ones(dem.shape, bool), GT, utm_wkt())
    assert np.array_equal(filled_dem, dem)
    assert (flowdir[1:8, 1:-1] != 0).all()
    assert follow(flowdir, 4, 15, 60)[-1][1] in (0, 29)  # off an end
