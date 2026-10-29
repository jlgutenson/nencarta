"""
Moving source reaches that the DEM shows are off their river onto it (nencarta.tasks.stream_relocation).

Every test builds a straight valley whose river flows east along row 50, with steepest-descent D8 directions, and
draws some of the source network on or beside the channels. Its heights are kept off whole multiples of 0.5 m, which
the relocation takes for water the DEM levels.
"""
import heapq

import geopandas as gpd
import numpy as np
import pytest
import shapely
from osgeo import osr
from shapely.geometry import LineString

from nencarta.tasks.move_streams import fill_and_flow_directions
from nencarta.tasks.stream_conflation import D8_OFFSETS
from nencarta.tasks.stream_relocation import (
    RelocationSettings,
    breach_dammed_rivers,
    relocate_misplaced_reaches,
)

CELL = 30.0
X0, Y0 = 500_000.0, 4_500_000.0
GT = (X0, CELL, 0.0, Y0, 0.0, -CELL)
ROWS, COLS = 80, 200
RIVER = 50
TRIBUTARY = 100
CELL_KM2 = CELL * CELL / 1e6
# The rules that keep a line where it is, off
ANY_PATH = RelocationSettings(min_channel_share=0.0, min_channel_gain=-1.0, min_length_ratio=0.0)


def utm_wkt() -> str:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32615)
    return srs.ExportToWkt()


def valley(with_tributary=False):
    """A valley falling 0.05 m a cell eastward and 0.5 m a cell from either side down to row 50; with a tributary
    valley down column 100 from the north edge."""
    rows, cols = np.mgrid[0:ROWS, 0:COLS]
    dem = 50 - 0.05 * cols + 0.5 * np.abs(rows - RIVER)
    if with_tributary:
        side = (rows < RIVER) & (np.abs(cols - TRIBUTARY) <= 6)
        dem[side] = np.minimum(dem[side], 50 - 0.05 * TRIBUTARY + 0.5 * (RIVER - rows[side]) * 0.4
                               + 0.6 * np.abs(cols[side] - TRIBUTARY))
    return dem.astype(np.float64) + 0.013


def d8(dem):
    """Steepest-descent D8 pointers (see D8_OFFSETS); 0 where no neighbour is lower."""
    padded = np.pad(dem, 1, constant_values=np.inf)
    best = np.zeros(dem.shape)
    flowdir = np.zeros(dem.shape, np.int16)
    for code, (dr, dc) in D8_OFFSETS.items():
        neighbour = padded[1 + dr:1 + dr + dem.shape[0], 1 + dc:1 + dc + dem.shape[1]]
        slope = (dem - neighbour) / np.hypot(dr, dc)
        better = slope > best
        best[better] = slope[better]
        flowdir[better] = code
    return flowdir


def fill(dem, epsilon=1e-4):
    """Priority-flood fill out to the grid's edge, each raised cell epsilon above the one it drains to."""
    out = dem.astype(np.float64).copy()
    done = np.zeros(dem.shape, bool)
    done[[0, -1], :] = done[:, [0, -1]] = True
    heap = [(out[r, c], r, c) for r, c in zip(*np.nonzero(done))]
    heapq.heapify(heap)
    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc in D8_OFFSETS.values():
            rr, cc = r + dr, c + dc
            if 0 <= rr < dem.shape[0] and 0 <= cc < dem.shape[1] and not done[rr, cc]:
                done[rr, cc] = True
                out[rr, cc] = max(out[rr, cc], z + epsilon)
                heapq.heappush(heap, (out[rr, cc], rr, cc))
    return out


def xy(row, col):
    return X0 + (col + 0.5) * CELL, Y0 - (row + 0.5) * CELL


def line(*cells):
    """A line through the centres of (row, col) cells, drawn downstream to upstream as GEOGLOWS draws them."""
    return LineString([xy(r, c) for r, c in cells][::-1])


def area_at(col):
    """Drainage area of the river cell in ``col``: every cell of the columns up to it drains into it."""
    return (col + 1) * ROWS * CELL_KM2


def network(rows):
    """Source reaches from (id, downstream id, geometry, km2) rows."""
    ids, ds, geoms, area = zip(*rows)
    return gpd.GeoDataFrame({"riverId": ids, "nextRiverId": ds, "area": area, "name": [f"r{i}" for i in ids]},
                            geometry=list(geoms), crs="EPSG:32615")


def relocate(src, dem, filled=None, settings=None, gt=GT, valid=None):
    """Relocate ``src`` on ``dem``, filled as ``filled`` (default: as it is), with its steepest-descent D8."""
    filled = dem if filled is None else filled
    return relocate_misplaced_reaches(src, d8(filled), filled, dem, gt, utm_wkt(), "riverId", "nextRiverId", "area",
                                      valid_mask=valid, settings=settings)


def cells_of(geometry, gt=GT):
    xy_ = shapely.get_coordinates(geometry)
    return np.floor((gt[3] - xy_[:, 1]) / CELL).astype(int), np.floor((xy_[:, 0] - gt[0]) / CELL).astype(int)


# ---------------------------------------------------------------------------------------------------------------
# Moving reaches onto the D8 paths their lines drain along
# ---------------------------------------------------------------------------------------------------------------

def test_a_line_up_its_valley_side_is_moved_onto_its_river():
    # 8 cells off: down the incised valley the path is on a channel all the way, and the line on none of it
    src = network([(1, -1, line((RIVER - 8, 5), (RIVER - 8, COLS - 5)), 15.0)])
    rows, cols = cells_of(relocate(src, valley()).geometry[0])
    # Drawn downstream to upstream: all but the head, which runs down the valley side from the line's first cell
    assert set(rows[:-9]) == {RIVER}
    assert cols.max() >= COLS - 10


def test_a_line_on_its_river_stays_where_it_is():
    # Its path is on the same channel: the DEM shows nothing the line misses
    src = network([(1, -1, line((RIVER, 5), (RIVER, COLS - 5)), 15.0)])
    assert relocate(src, valley()).geometry[0].equals(src.geometry[0])


def test_reaches_below_others_follow_their_river_and_tributaries_reach_it():
    # The river drawn up its valley side, split at the tributary's junction; the tributary drawn beside its valley
    dem = valley(with_tributary=True)
    src = network([
        (1, 3, line((RIVER - 8, 5), (RIVER - 8, TRIBUTARY - 6)), 8.0),
        (2, 3, line((5, TRIBUTARY - 4), (RIVER - 8, TRIBUTARY - 4)), 2.0),
        (3, -1, line((RIVER - 8, TRIBUTARY - 6), (RIVER - 8, COLS - 5)), 15.0),
    ])
    out = relocate(src, dem).set_index("riverId")
    rows, cols = cells_of(out.geometry[3])
    assert set(rows) == {RIVER}  # below a junction, the head is cut back to the river and carried up it
    trib_rows, trib_cols = cells_of(out.geometry[2])
    # Down its own valley, below its head (which runs down from the line's first cell into it)
    assert np.all(np.abs(trib_cols[(trib_rows > 10) & (trib_rows < RIVER - 10)] - TRIBUTARY) <= 1)
    end = shapely.Point(shapely.get_coordinates(out.geometry[2])[0])  # drawn downstream to upstream
    river = shapely.line_merge(shapely.union_all([out.geometry[1], out.geometry[3]]))
    assert end.distance(river) < 1.0


def test_below_a_junction_the_path_starts_at_it_though_the_dem_drains_less_than_flows_in():
    # The source says 100 km2 flows down reach 1, but the DEM's river drains a few km2 there (on a fan, the rest
    # could run down another channel). A tributary bringing 100 km2 across the north edge joins at column 100, where
    # the river first drains half of what the source says flows into reach 3; but the river by the junction drains all
    # the DEM brings, so reach 3's path starts there, not at column 100
    src = network([
        (1, 3, line((RIVER, 5), (RIVER, 60)), 100.0),
        (2, 3, line((-20, TRIBUTARY), (RIVER, TRIBUTARY)), 100.0),
        (3, -1, line((RIVER, 60), (RIVER - 8, 66), (RIVER - 8, COLS - 5)), 210.0),
    ])
    out = relocate(src, valley(with_tributary=True)).set_index("riverId")
    rows, cols = cells_of(out.geometry[3])
    assert set(rows) == {RIVER}
    assert cols.min() <= 62


def test_a_kept_line_is_carried_onto_the_moved_reach_it_flows_into():
    # The tributary is drawn down its own valley, so it stays; the river below it moves off the valley side
    dem = valley(with_tributary=True)
    src = network([
        (2, 3, line((5, TRIBUTARY), (RIVER - 8, TRIBUTARY)), 2.0),
        (3, -1, line((RIVER - 8, TRIBUTARY), (RIVER - 8, COLS - 5)), 15.0),
    ])
    out = relocate(src, dem).set_index("riverId")
    kept = shapely.get_coordinates(out.geometry[2])
    original = shapely.get_coordinates(src.geometry[0])
    assert np.allclose(kept[-1], original[-1])  # its head is where it was
    end = shapely.Point(kept[0])
    assert end.distance(out.geometry[3]) < 1.0


def test_a_moved_reach_keeps_its_line_above_its_path_where_the_reach_above_cant_reach_the_path():
    # As above, but with a pit in the tributary's channel 3 cells below its end: carried down D8, the tributary
    # stops in it, short of the river. Below the junction the river's path starts on the river, so the river keeps
    # its line from the junction to where its path starts, and the tributary still joins it
    dem = valley(with_tributary=True)
    dem[RIVER - 5, TRIBUTARY] -= 3.0
    src = network([
        (2, 3, line((5, TRIBUTARY), (RIVER - 8, TRIBUTARY)), 2.0),
        (3, -1, line((RIVER - 8, TRIBUTARY), (RIVER - 8, COLS - 5)), 15.0),
    ])
    out = relocate(src, dem).set_index("riverId")
    assert out.geometry[2].equals(src.set_index("riverId").geometry[2])
    end = shapely.Point(shapely.get_coordinates(out.geometry[2])[0])  # drawn downstream to upstream
    assert end.distance(out.geometry[3]) < 1.0
    rows, cols = cells_of(out.geometry[3])
    assert (rows == RIVER).sum() >= COLS - 5 - TRIBUTARY - 2  # on the river from the junction down
    assert rows.min() == RIVER - 8 and cols[rows < RIVER].min() == TRIBUTARY  # and its line's first cell above it


def test_a_reach_whose_path_is_neither_its_river_nor_leads_to_it_stays():
    # Reach 1 (30 km2) is drawn up the valley side. Its path down the valley drains a few km2 of it but runs on into
    # reach 2, so it moves; with a pit in the river at column 92 that drains nowhere, the path ends there, and stays
    src = network([
        (1, 2, line((RIVER - 8, 5), (RIVER - 8, 100)), 30.0),
        (2, -1, line((RIVER, 100), (RIVER, COLS - 5)), 45.0),
    ])
    drawn = src.set_index("riverId").geometry[1]
    assert not relocate(src, valley()).set_index("riverId").geometry[1].equals(drawn)
    dem = valley()
    dem[RIVER, 92] -= 3.0
    assert relocate(src, dem).set_index("riverId").geometry[1].equals(drawn)


def test_a_moved_reach_keeps_its_line_below_its_path_where_its_path_cant_reach_the_next_reach():
    # Reach 1 is drawn up the valley side, into reach 2, drawn down to the river and along it. Reach 1's path down the
    # river ends in a pit at column 92 that drains nowhere: it drains what reach 1 does, so it moves, and keeps its
    # line from where its path ends nearest it down to reach 2
    src = network([
        (1, 2, line((RIVER - 8, 5), (RIVER - 8, 100)), 8.0),
        (2, -1, line((RIVER - 8, 100), (RIVER, 100), (RIVER, COLS - 5)), 20.0),
    ])
    dem = valley()
    dem[RIVER, 92] -= 3.0
    out = relocate(src, dem).set_index("riverId")
    rows, cols = cells_of(out.geometry[1])
    assert set(cols[rows == RIVER]) >= set(range(10, 93))  # on the river down to the pit
    assert out.geometry[2].equals(src.set_index("riverId").geometry[2])
    start = shapely.Point(shapely.get_coordinates(out.geometry[2])[-1])  # drawn downstream to upstream
    assert start.distance(out.geometry[1]) < 1.0


def gully(dem, row, col0, col1):
    """``dem`` with a gully 1.4 m deep down ``row`` from ``col0`` to ``col1``, where it steps down to the river."""
    dem = dem.copy()
    dem[row, col0:col1 + 1] -= 1.4
    base = dem[RIVER, col1]
    dem[row + 1:RIVER, col1] = base + np.linspace(0.07, 0.04, RIVER - row - 1)
    return dem


@pytest.mark.parametrize("next_line, dem", [
    # Reach 2 starts 3 cells up the bank and only reaches the river 9 cells down: D8 carried reach 1 down the river,
    # beside reach 2's line, onto it there
    ([(RIVER - 3, 100), (RIVER, 110), (RIVER, COLS - 5)], valley()),
    # Reach 2 runs down a gully 3 cells up the bank, back into the river 40 cells down: D8 carried reach 1 down the
    # river past it, onto reach 3
    ([(RIVER - 3, 100), (RIVER - 3, 140)], gully(valley(), RIVER - 3, 100, 140)),
])
def test_a_moved_reach_whose_path_runs_on_past_the_next_reachs_start_ends_at_it(next_line, dem):
    # Reach 1 is drawn up the valley side to where reach 2 starts. Its path down the river passes reach 2's start 3
    # cells off; it ends there, and keeps its line from there to reach 2
    src = network([
        (1, 2, line((RIVER - 8, 5), (RIVER - 8, 95), (RIVER - 3, 100)), 8.0),
        (2, 3, line(*next_line), 20.0),
        (3, -1, line(next_line[-1], (RIVER, next_line[-1][1] + 1), (RIVER, COLS - 5)), 25.0),
    ])
    out = relocate(src, dem).set_index("riverId")
    drawn = src.set_index("riverId")
    assert out.geometry[2].equals(drawn.geometry[2])  # on a channel, it stays
    rows, cols = cells_of(out.geometry[1])
    assert set(cols[rows == RIVER]) >= set(range(10, 101))  # on the river down to reach 2's start
    end = shapely.Point(shapely.get_coordinates(out.geometry[1])[0])  # drawn downstream to upstream
    start = shapely.Point(shapely.get_coordinates(drawn.geometry[2])[-1])
    assert end.distance(start) < 1.0
    assert cols.max() <= 100


def test_a_reach_whose_path_runs_away_from_the_end_of_its_line_stays():
    # Reach 1 is drawn on the valley side flowing west into reach 2, against the valley, which falls east: its line's
    # cells drain down to the river and east, and its path runs from the line's west end east down the river. It
    # drains what reach 1 does, but ends far from its line's end and doesn't lead on into reach 2
    src = network([
        (1, 2, line((RIVER - 8, 150), (RIVER - 8, 40)), 15.0),
        (2, -1, line((RIVER - 8, 40), (RIVER - 8, 5)), 16.0),
    ])
    out = relocate(src, valley()).set_index("riverId")
    assert out.geometry[1].equals(src.set_index("riverId").geometry[1])


def test_rows_keep_their_columns_and_drawing_direction():
    src = network([(1, -1, line((RIVER - 8, 5), (RIVER - 8, COLS - 5)), 15.0)])
    out = relocate(src, valley())
    assert list(out.columns) == list(src.columns)
    assert out["name"].tolist() == ["r1"]
    first, last = shapely.get_coordinates(out.geometry[0])[[0, -1]]
    assert first[0] > last[0]  # still drawn downstream (east) to upstream


def test_a_moved_line_is_drawn_downstream_first_though_its_path_ends_farther_from_the_lines_end_than_it_starts():
    # A short line up the valley side: its path runs down the valley side from its upstream end, and along the river
    # to below its downstream end, 8 cells from it, twice as far as the path's head is
    src = network([(1, -1, line((RIVER - 8, 100), (RIVER - 8, 104)), 8.0)])
    rows, cols = cells_of(relocate(src, valley(), settings=ANY_PATH).geometry[0])
    assert (rows[0], cols[0]) == (RIVER, 104) and cols[-1] == 100


def test_a_line_on_ground_the_fill_flattened_stays_where_it_is():
    # A sill across the valley at column 150 ponds it: filled, its floor is a flat from about column 100 on, where the
    # D8 paths run wherever flat resolution sends them. The DEM shows no channel there for the path to follow.
    dem = valley()
    dem[RIVER - 12:RIVER + 13, 150] = 47.03
    filled, flowdir = fill_and_flow_directions(dem, np.ones(dem.shape, bool), GT, utm_wkt())
    assert (filled[RIVER - 3:RIVER + 4, 110:150] > dem[RIVER - 3:RIVER + 4, 110:150]).all()
    src = network([(1, -1, line((RIVER - 2, 105), (RIVER - 2, 148)), 15.0)])

    def moved(settings):
        out = relocate_misplaced_reaches(src, flowdir, filled, dem, GT, utm_wkt(), "riverId", "nextRiverId", "area",
                                         settings=settings)
        return not out.geometry[0].equals(src.geometry[0])

    assert moved(ANY_PATH)
    assert not moved(None)


def test_a_meandering_line_stays_where_it_is_when_its_path_cuts_the_meanders():
    # A floodplain 8 cells either side of a channel cut 0.5 m into it, rising 0.1 m a cell away from it: too coarse to
    # show the meanders, so the D8 path down the channel is a third shorter than the line, and less than a metre below
    # it
    rows, cols = np.mgrid[0:ROWS, 0:COLS]
    off = np.abs(rows - RIVER)
    dem = 50.013 - 0.05 * cols + 0.1 * np.minimum(off, 8) + 0.5 * np.maximum(off - 8, 0) - 0.5 * (off == 0)
    line_cols = np.arange(5, COLS - 5)
    line_rows = np.round(RIVER - 6 * np.sin(2 * np.pi * line_cols / 24)).astype(int)
    src = network([(1, -1, line(*zip(line_rows, line_cols)), 15.0)])
    assert not relocate(src, dem, settings=ANY_PATH).geometry[0].equals(src.geometry[0])
    lengths_only = RelocationSettings(min_channel_share=0.0, min_channel_gain=-1.0)
    assert relocate(src, dem, settings=lengths_only).geometry[0].equals(src.geometry[0])


def test_a_line_cutting_across_the_neck_of_a_bend_moves_onto_the_bend():
    # A channel cut 0.5 m into a floodplain (filled, so it drains up and down the loop's steep sides) loops 14 cells
    # north between columns 90 and 110; the line follows it but for a straight cut across the loop's neck. It is on
    # the channel for most of its length, so over the whole reach its path is on it only 17 points more; where they
    # differ, the line is off it and the path on it.
    from scipy.ndimage import distance_transform_edt
    rows, cols = np.mgrid[0:ROWS, 0:COLS]
    channel = np.zeros((ROWS, COLS), bool)
    channel[RIVER, :] = True
    xs = np.linspace(90, 110, 400)
    channel[RIVER, 91:110] = False
    channel[np.round(RIVER - 14 * np.sin(np.pi * (xs - 90) / 20)).astype(int), np.round(xs).astype(int)] = True
    off = distance_transform_edt(~channel)
    dem = 50.013 - 0.05 * cols + 0.1 * np.minimum(off, 8) + 0.5 * np.maximum(off - 8, 0) - 0.5 * channel
    src = network([(1, -1, line((RIVER, 40), (RIVER, 160)), 15.0)])
    out_rows, _ = cells_of(relocate(src, dem, filled=fill(dem)).geometry[0])
    assert out_rows.min() <= RIVER - 12
    held = relocate(src, dem, filled=fill(dem), settings=RelocationSettings(bend_channel_gain=None))
    assert held.geometry[0].equals(src.geometry[0])


def test_a_line_up_the_valley_side_and_back_moves_though_its_path_is_shorter():
    # Out 8 cells up the valley side, 4 m above the river, along it and back: longer than the river, but up the
    # valley side, not meandering on the floodplain
    src = network([(1, -1, line((RIVER, 40), (RIVER - 8, 48), (RIVER - 8, 72), (RIVER, 80)), 15.0)])
    rows, cols = cells_of(relocate(src, valley()).geometry[0])
    assert set(rows) == {RIVER} and cols.min() <= 42 and cols.max() >= 78
    # Held by the length alone
    held = relocate(src, valley(), settings=RelocationSettings(meander_max_rise_m=np.inf))
    assert held.geometry[0].equals(src.geometry[0])


# ---------------------------------------------------------------------------------------------------------------
# Rivers flowing in across the domain's edge
# ---------------------------------------------------------------------------------------------------------------

def inflow_network():
    """
    A river of 1000 km2 flows in over the west edge along row 50 (1); from column 120 the source draws it (2) on the
    valley side 10 cells north. The valley itself drains a few km2.
    """
    return network([
        (1, 2, line((RIVER, -20), (RIVER, 120)), 1000.0),
        (2, -1, line((RIVER, 120), (RIVER - 10, 126), (RIVER - 10, COLS - 1)), 1001.0),
    ])


def assert_follows_the_river_from_its_start(geometry):
    # Below the river flowing in, its path starts where the river drains what flows in, at the junction, not up the
    # valley side where the line's first cells drain down to the river
    rows, cols = cells_of(geometry)
    assert set(rows) == {RIVER}
    assert cols.min() <= 125


def test_a_river_flowing_in_across_the_edge_brings_its_drainage_area():
    out = relocate(inflow_network(), valley()).set_index("riverId")
    assert_follows_the_river_from_its_start(out.geometry[2])


def test_a_river_flowing_in_on_a_flat_level_with_the_edge_brings_its_water_down_the_river():
    # FABDEM flattens the river's water: up to column 80 the river runs on a flat at 46.013 m, level with the west
    # edge. Filled, the flat's west half drains back out across the edge; the water goes in where its path runs on
    # down the river.
    rows, cols = np.mgrid[0:ROWS, 0:COLS]
    dem = valley()
    dem[(np.abs(rows - RIVER) <= 2) & (cols <= 80)] = 46.013
    out = relocate(inflow_network(), dem, filled=fill(dem)).set_index("riverId")
    assert_follows_the_river_from_its_start(out.geometry[2])


def test_a_river_that_leaves_the_domain_and_comes_back_brings_its_water_back_in():
    # Nodata across the grid at columns 60-70: the river flows out of the domain there, and back in
    valid = np.ones((ROWS, COLS), bool)
    valid[:, 60:71] = False
    out = relocate(inflow_network(), valley(), valid=valid).set_index("riverId")
    assert_follows_the_river_from_its_start(out.geometry[2])


# ---------------------------------------------------------------------------------------------------------------
# Rivers the DEM dams
# ---------------------------------------------------------------------------------------------------------------

DAM = 120


def dammed_valley(crest, way_round=()):
    """
    A valley falling 0.1 m a cell eastward from 50 m at its west edge, dammed at columns 120-121 up to ``crest``,
    except on the rows ``way_round``. The river (1) flows in over the west edge to the dam; below it 2 follows the
    river for 5 cells, then is drawn on the valley side 10 cells north.
    """
    rows, cols = np.mgrid[0:ROWS, 0:COLS]
    dem = 50 - 0.1 * cols + 0.5 * np.abs(rows - RIVER)
    dammed = ~np.isin(np.arange(ROWS), way_round)
    dem[dammed, DAM:DAM + 2] = np.maximum(dem[dammed, DAM:DAM + 2], crest)
    src = network([
        (1, 2, line((RIVER, 0), (RIVER, DAM)), area_at(DAM)),
        (2, -1, line((RIVER, DAM), (RIVER, DAM + 5), (RIVER - 10, DAM + 11), (RIVER - 10, COLS - 1)),
         area_at(COLS - 1)),
    ])
    return dem, src


def breach(src, dem):
    return breach_dammed_rivers(src, dem, GT, utm_wkt(), "riverId", "nextRiverId")


def test_a_river_the_dem_dams_is_burned_through_and_found_below():
    # The dam rises 42 m over the river's line, and the valley has no way round it: filled, the valley above it
    # drains back out across the west edge, and below it no flow path drains what flows into 2
    dem, src = dammed_valley(crest=80)
    breached, reaches = breach(src, dem)
    assert reaches == [1]
    # Burned through the dam along the line, falling from the river's low above it to its low below; the rest of
    # the DEM is as it was
    assert (breached[RIVER, DAM:DAM + 2] < 38.2).all()
    assert np.all(np.diff(breached[RIVER, DAM - 1:DAM + 3]) <= 0)
    changed = breached != dem
    assert changed.sum() == 2 and changed[RIVER, DAM:DAM + 2].all()

    # The river's flow paths carry it on through the dam, so 2's path runs down the river from the dam; before, the
    # river below the dam drains only the valley below it, and the path starts some way down, with 2's line kept
    # above it, off the river where it bends to the valley side, to meet 1
    before = relocate(src, dem, filled=fill(dem)).set_index("riverId").geometry[2]
    after = relocate(src, breached, filled=fill(breached)).set_index("riverId").geometry[2]
    rows, cols = cells_of(before)
    assert (rows < RIVER).any() and cols[rows < RIVER].min() > DAM + 5
    rows, cols = cells_of(after)
    assert cols.min() <= DAM + 3 and set(rows) == {RIVER}


@pytest.mark.parametrize("crest, way_round", [
    (70, ()),  # 32 m over the river's line: lower than a dam must be
    (80, (60, 61, 62)),  # the valley's side has a way round it, 5 m up: the river may run there
])
def test_a_dam_lower_than_dam_height_or_with_a_way_round_is_left(crest, way_round):
    dem, src = dammed_valley(crest, way_round)
    assert breach(src, dem) == (None, [])


@pytest.mark.parametrize("col, height", [
    (DAM + 3, 45.0),  # a hump left at the dam's foot: the burn runs on past it, to where the river runs level
    (DAM - 1, 30.0),  # a pit at its head: the burn starts on the river above it, and the pit stays
])
def test_a_burn_starts_and_ends_where_the_river_runs_level(col, height):
    dem, src = dammed_valley(crest=80)
    dem[RIVER, col] = height
    breached, reaches = breach(src, dem)
    assert reaches == [1]
    burned = np.flatnonzero(breached[RIVER] != dem[RIVER])
    assert (breached[RIVER, DAM:DAM + 2] < 38.2).all()
    if height > 40:
        assert col in burned and breached[RIVER, col] < 38
        assert np.all(np.diff(breached[RIVER, DAM - 1:col + 2]) <= 0)
    else:
        assert breached[RIVER, col] == height
        assert burned.min() >= DAM - 2 and burned.max() <= DAM + 1


def test_a_line_alone_is_followed_downhill_for_dams():
    # A reach joined to no other: GEOGLOWS draws it downstream to upstream, and the heights say which way it runs
    dem, src = dammed_valley(crest=80)
    alone = network([(1, -1, line((RIVER, 0), (RIVER, DAM + 5)), area_at(DAM + 5))])
    breached, reaches = breach(alone, dem)
    assert reaches == [1]
    assert (breached[RIVER, DAM:DAM + 2] < 38.2).all()
