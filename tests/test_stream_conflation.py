"""
Moving a source stream network onto a DEM's D8 flow paths (nencarta.tasks.stream_conflation).

Every test builds a small D8 raster whose stream paths are known cell by cell -- hillslopes drain into the
nearest stream cell -- and a source network drawn near them, and checks where each source reach lands. Whatever
else a test checks, the result must keep FLDPLN's contract: each reach is one unbroken D8 path, its downstream
reach is the first one down that path from its last cell (or where that one drains, when it is entered at its last
few cells), and the stream raster is exactly those cells.
"""
from collections import deque

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from osgeo import osr
from shapely.geometry import LineString, MultiLineString

from nencarta.tasks.move_streams import _create_stream_info_table
from nencarta.tasks.stream_conflation import (
    D8_OFFSETS,
    ConflationSettings,
    FlowNetwork,
    conflate_to_flow_directions,
)

CELL = 30.0
X0, Y0 = 500_000.0, 4_500_000.0
GT = (X0, CELL, 0.0, Y0, 0.0, -CELL)
CODE = {offset: code for code, offset in D8_OFFSETS.items()}

# A threshold low enough that every cell a stream passes through is a channel cell, but most hillslope cells
# are not: a cell is 0.0009 km2, so this is about six cells' worth of drainage.
SETTINGS = ConflationSettings(min_channel_area_km2=0.005)


def utm_wkt() -> str:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32615)
    return srs.ExportToWkt()


def centre(cell, offset=(0.0, 0.0)):
    """Map coordinates of a (row, col) cell's centre, shifted by ``offset`` (rows, cols) in cells."""
    r, c = cell
    return X0 + (c + 0.5 + offset[1]) * CELL, Y0 - (r + 0.5 + offset[0]) * CELL


def straight(r0, c0, r1, c1):
    """The cells of a straight D8 path from (r0, c0) to (r1, c1), both included."""
    cells = [(r0, c0)]
    r, c = r0, c0
    while (r, c) != (r1, c1):
        r += (r1 > r) - (r1 < r)
        c += (c1 > c) - (c1 < c)
        cells.append((r, c))
    return cells


def flowdir_for(shape, paths, exits=()):
    """
    A D8 raster where each path in ``paths`` flows from cell to cell, and every other cell drains, by the
    shortest route, into the nearest path cell. A path ending on another path's cell joins it there. The last
    cell of a path that ends nowhere has no outflow, unless it is in ``exits``, a {cell: (d_row, d_col)} of
    cells that drain off the edge of the grid.
    """
    flowdir = np.zeros(shape, np.int16)
    on_path = np.zeros(shape, bool)
    for path in paths:
        for (r0, c0), (r1, c1) in zip(path[:-1], path[1:]):
            flowdir[r0, c0] = CODE[(r1 - r0, c1 - c0)]
        for cell in path:
            on_path[cell] = True
    for cell, offset in dict(exits).items():
        flowdir[cell] = CODE[offset]
    queue = deque(zip(*np.nonzero(on_path)))
    seen = on_path.copy()
    while queue:
        r, c = queue.popleft()
        for (dr, dc) in CODE:
            nr, nc = r - dr, c - dc  # the neighbour that would drain into (r, c) with offset (dr, dc)
            if 0 <= nr < shape[0] and 0 <= nc < shape[1] and not seen[nr, nc]:
                seen[nr, nc] = True
                flowdir[nr, nc] = CODE[(dr, dc)]
                queue.append((nr, nc))
    return flowdir


def line(cells, offset=(0.0, 0.0), reverse=False):
    pts = [centre(cell, offset) for cell in cells]
    return LineString(pts[::-1] if reverse else pts)


def source(rows, crs="EPSG:32615"):
    """rows: (id, downstream id, geometry, order)."""
    return gpd.GeoDataFrame(
        {"LINKNO": [r[0] for r in rows], "DSLINKNO": [r[1] for r in rows],
         "order": [r[3] if len(r) > 3 else 1 for r in rows], "extra": [f"attr{r[0]}" for r in rows]},
        geometry=[r[2] for r in rows], crs=crs)


def conflate(src, flowdir, settings=SETTINGS, **kwargs):
    return conflate_to_flow_directions(src, flowdir, GT, utm_wkt(), "LINKNO", "DSLINKNO", order_col="order",
                                       settings=settings, **kwargs)


def cells_of(result, reach_id):
    """The raster cells carrying ``reach_id``, from upstream to downstream along the reach's line."""
    row = result.streams.set_index("LINKNO").loc[reach_id]
    out = []
    for x, y in row.geometry.coords:
        out.append((int((Y0 - y) // CELL), int((x - X0) // CELL)))
    return out


def ds_of(result, reach_id):
    return int(result.streams.set_index("LINKNO").loc[reach_id, "DSLINKNO"])


def assert_fldpln_contract(result, flowdir, tmp_path):
    """Every reach is one D8 path, drains into the reach it says it does, and the raster is exactly its cells."""
    raster = result.raster
    nrows, ncols = raster.shape

    def step(cell):
        code = int(flowdir[cell])
        if code not in D8_OFFSETS:
            return None
        d_row, d_col = D8_OFFSETS[code]
        r, c = cell[0] + d_row, cell[1] + d_col
        return (r, c) if 0 <= r < nrows and 0 <= c < ncols else None

    labelled = set()
    for _, row in result.streams.iterrows():
        rid = int(row.LINKNO)
        cells = [(int((Y0 - y) // CELL), int((x - X0) // CELL)) for x, y in row.geometry.coords]
        assert len(cells) >= 2
        for a, b in zip(cells[:-1], cells[1:]):
            d_row, d_col = D8_OFFSETS[int(flowdir[a])]
            assert (a[0] + d_row, a[1] + d_col) == b, f"reach {rid} leaves its D8 path at {a}"
        for cell in cells:
            assert raster[cell] == rid
            labelled.add(cell)
        # Its downstream reach is the first one down its D8 path -- across the gap a reach that ends beside the
        # river it joins leaves, and past any it enters only at their last few cells
        stated, cell = int(row.DSLINKNO), step(cells[-1])
        gap = 0
        while stated != -1 and cell is not None and raster[cell] == 0 and gap < 30:
            cell, gap = step(cell), gap + 1
        while cell is not None and raster[cell] not in (0, stated):
            passed, n = int(raster[cell]), 0
            while cell is not None and raster[cell] == passed:
                cell, n = step(cell), n + 1
            assert n <= 4, f"reach {rid} says it drains into {stated}, D8 says {passed}"
        below = int(raster[cell]) if cell is not None else 0
        assert below == max(stated, 0), f"reach {rid} says it drains into {stated}, D8 says {below or -1}"
    assert set(zip(*np.nonzero(raster))) == labelled

    # And FLDPLN's walk of each reach covers exactly its cells
    flowdir_file = tmp_path / "flowdir.tif"
    if not flowdir_file.exists():
        from osgeo import gdal
        ds = gdal.GetDriverByName("GTiff").Create(str(flowdir_file), ncols, nrows, 1, gdal.GDT_Int16)
        ds.WriteArray(flowdir)
        ds = None
    table_file = tmp_path / "stream_info.parquet"
    _create_stream_info_table(table_file, raster, flowdir_file)
    table = pd.read_parquet(table_file).set_index("stream_id")
    for rid, n_cells in result.streams.set_index("LINKNO").geometry.apply(lambda g: len(g.coords)).items():
        assert table.at[rid, "length"] == n_cells


# ---------------------------------------------------------------------------------------------------------------
# One reach
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def river_east():
    """A river flowing east along row 10 of a 21 x 40 grid and off its east edge."""
    path = straight(10, 0, 10, 39)
    return path, flowdir_for((21, 40), [path], exits={(10, 39): (0, 1)})


def test_a_reach_on_its_d8_path_keeps_every_cell(river_east, workspace_tmp):
    path, flowdir = river_east
    result = conflate(source([(5, -1, line(path))]), flowdir)

    assert cells_of(result, 5) == path
    assert ds_of(result, 5) == -1
    assert result.streams.loc[0, "extra"] == "attr5"  # source attributes ride along
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_line_drawn_downstream_to_upstream_is_turned_around(river_east, workspace_tmp):
    # TDX-Hydro and GEOGLOWS draw their lines from the outlet up
    path, flowdir = river_east
    result = conflate(source([(5, -1, line(path, reverse=True))]), flowdir)

    assert cells_of(result, 5) == path
    assert_fldpln_contract(result, flowdir, workspace_tmp)


@pytest.mark.parametrize("offset", [(1.0, 0.0), (-1.4, 0.0), (0.6, 0.3)])
def test_a_line_beside_the_channel_moves_onto_it(river_east, offset, workspace_tmp):
    path, flowdir = river_east
    result = conflate(source([(5, -1, line(path, offset=offset))]), flowdir)

    cells = cells_of(result, 5)
    assert all(r == 10 for r, _ in cells)
    assert len(cells) >= len(path) - 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_line_far_from_any_channel_is_still_placed(river_east, workspace_tmp):
    # Every reach on the DEM is placed: this one on a hillslope flow path near it, off the river
    path, flowdir = river_east
    far = straight(0, 0, 0, 39)  # ten cells north, on a ridge of hillslope cells
    result = conflate(source([(5, -1, line(path)), (6, -1, line(far))]), flowdir)

    assert cells_of(result, 5) == path
    assert len(cells_of(result, 6)) >= 2
    assert all(row < 10 for row, _ in cells_of(result, 6))
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_reaches_drawn_against_the_flow_are_placed_the_way_the_dem_runs(river_east, workspace_tmp):
    # The D8 path runs east; the network says the east half drains into the west half, so as drawn each line runs
    # west, against the flow. Each is placed on the cells under it, and drains where the DEM sends it.
    path, flowdir = river_east
    upstream_end = straight(10, 0, 10, 19)
    result = conflate(source([(1, 2, line(path[20:])), (2, -1, line(upstream_end[::-1]))]), flowdir)

    assert cells_of(result, 2) == path[:20]
    assert cells_of(result, 1) == path[20:]
    assert ds_of(result, 2) == 1 and ds_of(result, 1) == -1
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_geographic_grid(workspace_tmp):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    arcsec = 1 / 3600
    gt = (-95.0, arcsec, 0.0, 41.0, 0.0, -arcsec)
    path = straight(10, 0, 10, 39)
    flowdir = flowdir_for((21, 40), [path], exits={(10, 39): (0, 1)})
    pts = [(gt[0] + (c + 0.5) * arcsec, gt[3] - (r + 0.5) * arcsec) for r, c in path]
    src = gpd.GeoDataFrame({"LINKNO": [5], "DSLINKNO": [-1]}, geometry=[LineString(pts)], crs="EPSG:4326")

    result = conflate_to_flow_directions(src, flowdir, gt, srs.ExportToWkt(), "LINKNO", "DSLINKNO",
                                         settings=ConflationSettings(min_channel_area_km2=0.003))

    assert len(result.streams) == 1
    assert int(result.raster[10, :].astype(bool).sum()) == 40


# ---------------------------------------------------------------------------------------------------------------
# Where reaches meet
# ---------------------------------------------------------------------------------------------------------------

def test_a_break_without_a_confluence_splits_the_d8_path(river_east, workspace_tmp):
    # Most of a network's reach breaks are not confluences (GEOGLOWS merges its first-order streams away)
    path, flowdir = river_east
    upper, lower = path[:21], path[20:]
    result = conflate(source([(1, 2, line(upper)), (2, -1, line(lower))]), flowdir)

    up, down = cells_of(result, 1), cells_of(result, 2)
    assert up + down == path
    assert abs(len(up) - 20) <= 1
    assert ds_of(result, 1) == 2
    assert_fldpln_contract(result, flowdir, workspace_tmp)


@pytest.fixture
def confluence():
    """
    A main stem down column 20 of a 40 x 41 grid, off the south edge, and a tributary from the north-east that
    the source network joins to it at row 15. The tributary's D8 path follows its source line, but the DEM keeps
    it beside the main stem, one column east, until row ``join``.
    """
    def build(join):
        main = straight(0, 20, 39, 20)
        tributary = straight(3, 32, 14, 21) + [(row, 21) for row in range(15, join)] + [(join, 20)]
        flowdir = flowdir_for((40, 41), [main, tributary], exits={(39, 20): (1, 0)})
        src = source([
            (1, 3, line(main[:16]), 2),     # main stem down to the source confluence
            (2, 3, line(straight(3, 32, 15, 20)), 1),
            (3, -1, line(main[15:]), 2),    # main stem below it
        ])
        return src, flowdir, main, tributary
    return build


def test_a_confluence_where_the_source_network_has_it(confluence, workspace_tmp):
    src, flowdir, main, tributary = confluence(join=15)
    result = conflate(src, flowdir)

    assert ds_of(result, 1) == 3 and ds_of(result, 2) == 3
    assert cells_of(result, 3)[0] == (15, 20)
    assert cells_of(result, 2) == tributary[:-1]
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_tributary_the_dem_runs_beside_its_river_ends_where_it_comes_beside_it(confluence, workspace_tmp):
    src, flowdir, main, tributary = confluence(join=22)
    result = conflate(src, flowdir)

    # Its own D8 path runs a cell from the river's down to row 22, but those cells would only repeat the river's:
    # it ends at the first of them, and still drains into the river below the confluence
    assert cells_of(result, 2) == tributary[:tributary.index((14, 21)) + 1]
    assert ds_of(result, 2) == 3 and ds_of(result, 1) == 3
    assert cells_of(result, 3)[0] == (15, 20)
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_beside_a_smaller_one_keeps_its_cells(confluence, workspace_tmp):
    # The same paths, with the tributary the larger river: the main stem's cells beside it stay
    src, flowdir, main, tributary = confluence(join=22)
    src.loc[src.LINKNO == 2, "order"] = 3
    result = conflate(src, flowdir)

    assert cells_of(result, 2)[-1] == (21, 21)
    assert cells_of(result, 1)[-1] in {(14, 20), (15, 20)}
    assert ds_of(result, 1) == 3 and ds_of(result, 2) == 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_river_drawn_down_a_tributary_starts_where_the_river_joins_it(confluence, workspace_tmp):
    # The source network draws the river below the confluence (3) down the tributary's D8 path, a cell east of the
    # river's, from row 14 to where the two paths meet at row 22. Started there, it would flood the tributary's
    # cells as deep as the river. It starts where the river's own flow joins it instead
    src, flowdir, main, tributary = confluence(join=22)
    src.loc[src.LINKNO == 3, "geometry"] = line(tributary[tributary.index((14, 21)):] + main[23:])
    src["area"] = src.LINKNO.map({1: 100.0, 2: 5.0, 3: 106.0})
    result = conflate(src, flowdir, area_col="area")

    assert cells_of(result, 3)[0] == (22, 20)
    assert all(col == 20 for _, col in cells_of(result, 3))
    assert cells_of(result, 1)[-1] == (21, 20)
    assert ds_of(result, 1) == 3 and ds_of(result, 2) == 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_two_reaches_the_dem_joins_early_start_their_river_where_they_meet(workspace_tmp):
    # The D8 paths of the main stem and its tributary meet at row 12, three cells above the source confluence:
    # below the meeting point both flows run together, so that is where the reach they join has to start
    main = straight(0, 20, 39, 20)
    tributary = straight(3, 29, 12, 20)
    flowdir = flowdir_for((40, 41), [main, tributary], exits={(39, 20): (1, 0)})
    src = source([
        (1, 3, line(main[:16]), 2),
        (2, 3, line(straight(3, 29, 9, 23) + straight(10, 23, 15, 20)), 1),  # drawn into row 15
        (3, -1, line(main[15:]), 2),
    ])
    result = conflate(src, flowdir)

    assert cells_of(result, 3)[0] == (12, 20)
    assert cells_of(result, 1)[-1] == (11, 20)
    assert ds_of(result, 1) == 3 and ds_of(result, 2) == 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_tributary_the_dem_routes_alongside_the_river_follows_its_own_path(workspace_tmp):
    # The DEM keeps the tributary in its own channel beside the main stem for ten cells before the two meet
    main = straight(0, 20, 39, 20)
    tributary = straight(5, 30, 12, 23) + straight(13, 23, 22, 23) + straight(23, 22, 24, 21)
    tributary = tributary + [(25, 20)]
    flowdir = flowdir_for((40, 41), [main, tributary], exits={(39, 20): (1, 0)})
    src = source([
        (1, 3, line(main[:13]), 2),
        (2, 3, line(straight(5, 30, 12, 20)), 1),  # the source joins it at row 12
        (3, -1, line(main[12:]), 2),
    ])
    result = conflate(src, flowdir)

    assert cells_of(result, 2)[-1] == (24, 21)
    assert ds_of(result, 2) == 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_drawn_far_from_its_river_still_takes_its_place_in_it(river_east, workspace_tmp):
    # Ten cells off, the middle reach is too far for the first search, and is found by the wider one
    path, flowdir = river_east
    nonsense = LineString([centre((0, 20)), centre((0, 25))])
    src = source([(1, 2, line(path[:15])), (2, 3, nonsense), (3, -1, line(path[25:]))])
    result = conflate(src, flowdir)

    on_river = [cell for rid in (1, 2, 3) for cell in cells_of(result, rid) if cell[0] == 10]
    assert on_river == path
    assert cells_of(result, 3) == path[25:]
    assert ds_of(result, 1) == 2 and ds_of(result, 2) == 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_wide_rivers_reach_whose_flow_path_wanders_off_its_line_is_matched(workspace_tmp):
    # Across the flat water of a wide river D8 can run far from the line: here the river's flow path swings seven
    # cells south of the middle reach's line, beyond the search radius. (A threshold that leaves the hillslopes
    # out, or the reach would match a hillslope's flow path at its end.)
    path = straight(10, 0, 10, 9) + straight(11, 10, 16, 15) + straight(17, 15, 17, 24) + \
        straight(16, 25, 11, 30) + straight(10, 31, 10, 49)
    flowdir = flowdir_for((31, 50), [path], exits={(10, 49): (0, 1)})
    line_cells = straight(10, 0, 10, 49)
    src = source([(1, 2, line(line_cells[:10])), (2, 3, line(line_cells[9:32])), (3, -1, line(line_cells[31:]))])
    result = conflate(src, flowdir, settings=ConflationSettings(min_channel_area_km2=0.01))

    assert cells_of(result, 1) + cells_of(result, 2) + cells_of(result, 3) == path
    assert (17, 20) in cells_of(result, 2)
    assert ds_of(result, 1) == 2 and ds_of(result, 2) == 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_the_flow_from_above_bypasses_is_moved_onto_that_flow(workspace_tmp):
    # The source network sends the river (1) into reach 2 and on into 3, but draws 2 down a side channel. On the DEM
    # the river carries on down the main channel to 3, which the side channel only joins at 3's head: 2 is moved
    # onto the river's flow path, and the side channel is left to no reach.
    main = straight(10, 0, 10, 49)
    side = straight(20, 12, 20, 34) + straight(19, 35, 11, 43)
    flowdir = flowdir_for((31, 50), [main, side + [(10, 44)]], exits={(10, 49): (0, 1)})
    src = source([(1, 2, line(main[:12]), 2), (2, 3, line(side), 2), (3, -1, line(main[45:]), 2)])
    src["area"] = [100.0, 105.0, 110.0]
    result = conflate(src, flowdir, area_col="area")

    assert cells_of(result, 1) + cells_of(result, 2) + cells_of(result, 3) == main
    assert ds_of(result, 1) == 2 and ds_of(result, 2) == 3
    assert not result.raster[20, 12:35].any()
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_starts_where_the_reach_flowing_into_it_ends_however_far_its_match_starts_below(river_east,
                                                                                                 workspace_tmp):
    # Fifteen cells between the two matches, farther than the reach above may carry on to reach the one below
    path, flowdir = river_east
    src = source([(1, 2, line(path[:10])), (2, -1, line(path[25:]))])
    src["area"] = [100.0, 110.0]
    settings = ConflationSettings(min_channel_area_km2=0.005, max_extension_m=300.0)
    result = conflate(src, flowdir, settings=settings, area_col="area")

    assert cells_of(result, 1) == path[:10]
    assert cells_of(result, 2) == path[10:]
    assert ds_of(result, 1) == 2
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_two_source_lines_on_one_channel_never_share_a_cell(river_east, workspace_tmp):
    # Two unrelated source reaches drawn down the same valley: the larger keeps the channel, and the other gets a
    # short flow path of its own beside it
    path, flowdir = river_east
    src = source([(1, -1, line(path, offset=(0.4, 0)), 3), (2, -1, line(path[5:30], offset=(-0.4, 0)), 1)])
    result = conflate(src, flowdir)

    assert cells_of(result, 1) == path
    assert len(cells_of(result, 2)) >= 2
    assert not set(cells_of(result, 2)) & set(path)
    assert ds_of(result, 2) == 1
    assert_fldpln_contract(result, flowdir, workspace_tmp)


@pytest.fixture
def siblings():
    """
    A river (2) down column 20 of a 40 x 41 grid, off the south edge, and a tributary (1) from the north-east,
    both drawn down to row 30, where the source network sends them into a reach (3) two cells long. The
    tributary's D8 path runs beside the river, one column east, until it joins it at row ``join``. With
    ``below``, the reach after that one (4) starts at row 32. With ``lake``, a lake covers rows 30 and 31, so
    reach 3 can't be placed and the river ends at its shore. Returns the sources, the D8 raster, the river's
    cells and the lake mask (None without one).
    """
    def build(join, tributary_ds=3, below=False, lake=False):
        river = straight(0, 20, 39, 20)
        tributary = straight(3, 32, 14, 21) + [(row, 21) for row in range(15, join)] + [(join, 20)]
        flowdir = flowdir_for((40, 41), [river, tributary], exits={(39, 20): (1, 0)})
        rows = [
            (1, tributary_ds, line(straight(3, 32, 14, 21) + [(row, 21) for row in range(15, 30)] + [(30, 20)]), 1),
            (2, 3, line(river[:31]), 2),
            (3, 4 if below else -1, line(river[30:32]), 2),
        ]
        if below:
            rows.append((4, -1, line(river[32:]), 2))
        lake_mask = None
        if lake:
            lake_mask = np.zeros((40, 41), bool)
            lake_mask[30:32, 15:26] = True
        return source(rows), flowdir, river, lake_mask
    return build


@pytest.mark.parametrize("below", [False, True])
def test_two_reaches_the_dem_joins_just_above_the_reach_they_drain_into_both_drain_into_it(siblings, below,
                                                                                            workspace_tmp):
    # The DEM joins them a cell above the river's last: the short reach they drain into starts there
    src, flowdir, river, _ = siblings(join=29, below=below)
    result = conflate(src, flowdir)

    assert cells_of(result, 3)[0] == (29, 20)
    # The tributary is drawn only to where it comes beside the river (see _drawn_cells)
    assert cells_of(result, 1)[-1] == (14, 21) and cells_of(result, 2)[-1] == (28, 20)
    assert ds_of(result, 1) == 3 and ds_of(result, 2) == 3
    assert ds_of(result, 3) == (4 if below else -1)
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_entering_its_siblings_last_cells_drains_where_its_sibling_does(siblings, workspace_tmp):
    # The DEM joins the two at the river's last cell, on the shore of the lake their shared reach is in. Recorded
    # as the river's tributary, it would join an outlet at its very end, and ARC would read that end as the
    # river's head and turn the river round.
    src, flowdir, river, lake = siblings(join=29, lake=True)
    result = conflate(src, flowdir, lake_mask=lake)

    assert 3 not in set(result.streams.LINKNO)
    assert cells_of(result, 1)[-1] == (14, 21)
    assert cells_of(result, 2)[-1] == (29, 20)
    assert ds_of(result, 2) == -1
    assert ds_of(result, 1) == ds_of(result, 2)
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_joining_its_sibling_mid_reach_is_its_tributary(siblings, workspace_tmp):
    # The DEM joins the two fifteen cells above the lake: below there the river carries both
    src, flowdir, river, lake = siblings(join=15, lake=True)
    result = conflate(src, flowdir, lake_mask=lake)

    assert cells_of(result, 1)[-1] == (14, 21)
    assert ds_of(result, 1) == 2
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_the_source_network_sends_into_a_reachs_last_cells_joins_it(siblings, workspace_tmp):
    src, flowdir, river, _ = siblings(join=29, tributary_ds=2)
    result = conflate(src, flowdir)

    assert cells_of(result, 1)[-1] == (14, 21)
    assert ds_of(result, 1) == 2
    assert_fldpln_contract(result, flowdir, workspace_tmp)


# ---------------------------------------------------------------------------------------------------------------
# Endorheic basins, lakes and the edge of the domain
# ---------------------------------------------------------------------------------------------------------------

def test_an_endorheic_reach_ends_where_its_source_line_ends(workspace_tmp):
    # A filled DEM spills the closed basin at (20, 15) over its rim and on into the river along row 30; the
    # source network knows the basin is closed. Its reach must stop at the sink, and nothing may carry its id over
    # the spill path.
    river = straight(30, 0, 30, 39)
    basin = straight(5, 15, 20, 15)
    spill = straight(20, 15, 29, 15)
    flowdir = flowdir_for((40, 40), [river, basin + spill[1:] + [(30, 15)]], exits={(30, 39): (0, 1)})
    src = source([(1, -1, line(basin), 1), (7, -1, line(river), 2)])
    result = conflate(src, flowdir)

    basin_cells = cells_of(result, 1)
    assert basin_cells[-1][0] in (19, 20, 21)
    assert ds_of(result, 1) == -1
    assert not result.raster[23:30, 15].any()
    assert cells_of(result, 7) == river
    assert_fldpln_contract(result, flowdir, workspace_tmp)


@pytest.fixture
def river_through_lake():
    """A river flowing east along row 10 through a lake covering columns 15 to 24."""
    path = straight(10, 0, 10, 39)
    flowdir = flowdir_for((21, 40), [path], exits={(10, 39): (0, 1)})
    lake = np.zeros((21, 40), bool)
    lake[6:15, 15:25] = True
    src = source([
        (1, 2, line(path[:18])),     # flows into the lake
        (2, 3, line(path[17:23])),   # inside it
        (3, -1, line(path[22:])),    # leaves it
    ])
    return path, flowdir, lake, src


def test_a_lake_cuts_the_network_at_its_shores(river_through_lake, workspace_tmp):
    path, flowdir, lake, src = river_through_lake
    result = conflate(src, flowdir, lake_mask=lake)

    assert set(result.streams.LINKNO) == {1, 3}
    assert cells_of(result, 1)[-1] == (10, 14)
    assert cells_of(result, 3)[0] == (10, 25)
    assert not (result.raster.astype(bool) & lake).any()
    assert ds_of(result, 1) == -1
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_without_a_lake_mask_the_lake_reach_is_kept(river_through_lake, workspace_tmp):
    path, flowdir, lake, src = river_through_lake
    result = conflate(src, flowdir)

    assert set(result.streams.LINKNO) == {1, 2, 3}
    assert cells_of(result, 1) + cells_of(result, 2) + cells_of(result, 3) == path


def lake_outlet_dem(steps):
    """
    A filled DEM for river_through_lake: the lake level at 120 m, the river leaving it at that level on its shore
    (column 25) and falling ``steps`` (metres a cell) from there, then 0.05 m a cell. Higher ground elsewhere.
    """
    dem = np.full((21, 40), 150.0)
    dem[10, :15] = 121.0 + 0.05 * np.arange(15)[::-1]
    dem[10, 15:25] = 120.0
    z = 120.0
    for k, col in enumerate(range(25, 40)):
        dem[10, col] = z
        z -= steps[k] if k < len(steps) else 0.05
    return dem


def test_a_reach_leaving_a_lake_over_a_dam_starts_at_its_foot(river_through_lake, workspace_tmp):
    # Two cells of dam face below the lake's shore, 17 m in all: the reach starts where the river does
    path, flowdir, lake, src = river_through_lake
    result = conflate(src, flowdir, lake_mask=lake, dem=lake_outlet_dem([8.0, 9.0]))

    assert cells_of(result, 3) == path[27:]
    assert not result.raster[10, 25:27].any()
    assert cells_of(result, 1) == path[:15]
    assert_fldpln_contract(result, flowdir, workspace_tmp)


@pytest.mark.parametrize("steps, settings", [
    ([], SETTINGS),  # a lake spilling straight into its river
    ([0.6] * 6, SETTINGS),  # rapids below it, but no step a dam would make
    # a steep stream that keeps falling a metre and a half a cell past the outlet
    ([1.5] * 14, ConflationSettings(min_channel_area_km2=0.005, max_outlet_m=150.0)),
])
def test_a_reach_leaving_a_lake_without_a_dam_starts_on_its_shore(river_through_lake, steps, settings,
                                                                   workspace_tmp):
    path, flowdir, lake, src = river_through_lake
    result = conflate(src, flowdir, settings=settings, lake_mask=lake, dem=lake_outlet_dem(steps))

    assert cells_of(result, 3) == path[25:]
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_reach_starting_away_from_a_lake_keeps_its_drop(river_through_lake, workspace_tmp):
    # The same drop, but the lake ends five cells above the reach's first cell
    path, flowdir, lake, _ = river_through_lake
    lake[:, 20:25] = False
    src = source([(1, 3, line(path[:18])), (3, -1, line(path[25:]))])
    result = conflate(src, flowdir, lake_mask=lake, dem=lake_outlet_dem([8.0, 9.0]))

    assert cells_of(result, 3) == path[25:]
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_river_flowing_in_across_the_edge_is_matched_from_the_edge(workspace_tmp):
    # With a channel threshold larger than the strip of domain the river has crossed, the river's first cells
    # drain too little to count as channel -- unless its inflow is counted
    path = straight(10, 0, 10, 39)
    flowdir = flowdir_for((21, 40), [path], exits={(10, 39): (0, 1)})
    entering = LineString([centre((10, -8)), centre((10, 39))])  # starts outside the grid
    settings = ConflationSettings(min_channel_area_km2=0.2)
    result = conflate(source([(5, -1, entering)]), flowdir, settings=settings)

    assert cells_of(result, 5)[0] == (10, 0)
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def river_from_the_west():
    """
    A river flowing in over the west edge along rows 20-21 of a 41 x 45 grid, which the DEM flattens at 0 m between
    4 m banks: big enough for its D8 path to run well into the grid (see _runs_inward). Its flow directions and DEM.
    """
    flowdir = flowdir_for((41, 45), [straight(20, 0, 20, 44)], exits={(20, 44): (0, 1)})
    dem = np.full((41, 45), 4.0)
    dem[20:22] = 0.0
    return flowdir, dem


@pytest.mark.parametrize("with_dem", [True, False])
def test_a_river_whose_line_crosses_the_edge_on_its_bank_comes_in_at_the_river(with_dem, workspace_tmp):
    # The line runs along row 19.6, from 8 cells west of the grid: it crosses the edge on the bank cell (19, 0), which
    # drains down into the river at (20, 0). The river's water comes in at the river, and the reach starts there, not
    # on the bank. The channel threshold is above what the hillslopes drain, so the only channel is the river's path
    # from where its water comes in.
    from nencarta.tasks.stream_conflation import (
        _inflow_cells,
        _sample_lines,
        prepare_source_network,
    )
    flowdir, dem = river_from_the_west()
    dem = dem if with_dem else None
    src = source([(5, -1, LineString([centre((19, -8), (0.1, -0.5)), centre((19, 44), (0.1, 0.5))]))])

    flow = FlowNetwork(flowdir, GT, utm_wkt())
    lines = prepare_source_network(src, "LINKNO", "DSLINKNO", "order", None, flow)
    _, _, _, offsets, cell_of_sample, inside = _sample_lines(lines.geometry, flow, CELL)
    _, entry = _inflow_cells(offsets, inside, cell_of_sample, flow, None if dem is None else dem.ravel())
    assert divmod(int(entry[0]), 45) == (20, 0)

    result = conflate(src, flowdir, settings=ConflationSettings(min_channel_area_km2=1.0), dem=dem)
    assert cells_of(result, 5)[0] == (20, 0)
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_river_does_not_come_in_where_the_water_would_leave_the_domain():
    # A cell on the edge below the river's, beside it, drains straight back out of the grid
    from nencarta.tasks.stream_conflation import _river_at_edge
    flowdir, dem = river_from_the_west()
    flowdir[22, 0] = CODE[(0, -1)]
    dem[22, 0] = -1.0
    flow = FlowNetwork(flowdir, GT, utm_wkt())

    assert _river_at_edge(flow, 20 * 45, dem.ravel()) == 20 * 45


def test_nodata_stops_the_network(river_east, workspace_tmp):
    path, flowdir = river_east
    valid = np.ones(flowdir.shape, bool)
    valid[:, 30:] = False  # ocean
    result = conflate(source([(5, -1, line(path))]), flowdir, valid_mask=valid)

    assert cells_of(result, 5)[-1] == (10, 29)
    assert not result.raster[:, 30:].any()


# ---------------------------------------------------------------------------------------------------------------
# Messy inputs
# ---------------------------------------------------------------------------------------------------------------

def test_messy_source_networks_are_tolerated(river_east, workspace_tmp):
    path, flowdir = river_east
    rows = [
        (1, 2, line(path[:10])),
        (2, 1, line(path[9:20])),                          # a cycle with reach 1
        (3, 3, line(path[19:30])),                         # drains into itself
        (4, 999, line(path[29:])),                         # drains into a reach that isn't here
        (4, -1, line(path[29:])),                          # listed twice
        (0, -1, line(path[:5])),                           # an id that can't label a raster cell
        (-7, -1, line(path[:5])),
        (8, -1, None),                                     # no geometry
        (9, -1, LineString()),                             # empty geometry
        (10, -1, MultiLineString([line(path[:3]), line(path[30:33])])),  # in two pieces
    ]
    src = gpd.GeoDataFrame({"LINKNO": [r[0] for r in rows], "DSLINKNO": [r[1] for r in rows], "order": 1},
                           geometry=[r[2] for r in rows], crs="EPSG:32615")
    result = conflate(src, flowdir)

    assert {1, 2, 3, 4} <= set(result.streams.LINKNO)
    assert result.streams.LINKNO.is_unique
    assert not (result.raster < 0).any()
    assert_fldpln_contract(result, flowdir, workspace_tmp)


@pytest.mark.parametrize("has_downstream", [True, False])
def test_a_y_shaped_reach_is_matched_along_its_trunk_and_longer_branch(has_downstream, workspace_tmp):
    # TDX-Hydro folds first-order streams into the reach below, leaving reaches drawn as three pieces that
    # line_merge can't join: two headwater branches and the trunk below where they meet. The trunk is what has
    # to be matched -- it carries the flow -- and it is not the longest piece.
    trunk = straight(20, 20, 20, 39)
    long_branch = straight(2, 2, 20, 20)
    short_branch = straight(30, 10, 20, 20)
    main = long_branch + trunk[1:]
    paths = [main, short_branch]
    exits = {(20, 39): (0, 1)}
    rows = []
    y_reach = MultiLineString([line(trunk, reverse=True), line(long_branch, reverse=True),
                               line(short_branch, reverse=True)])
    if has_downstream:
        below = straight(20, 39, 20, 49)
        paths = [long_branch + trunk[1:] + below[1:], short_branch]
        exits = {(20, 49): (0, 1)}
        rows.append((2, -1, line(below), 1))
    rows.insert(0, (1, 2 if has_downstream else -1, y_reach, 1))
    flowdir = flowdir_for((41, 50 if has_downstream else 40), paths, exits=exits)
    result = conflate(source(rows), flowdir)

    cells = cells_of(result, 1)
    assert cells[0] == long_branch[0]
    if has_downstream:
        assert cells[-1] == trunk[-2]  # the reach below starts at the trunk's last cell
        assert ds_of(result, 1) == 2
    else:
        assert cells[-1] == trunk[-1]
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_y_shaped_reach_whose_main_stem_is_off_the_grid_is_matched_along_the_piece_on_it(river_east,
                                                                                          workspace_tmp):
    # Its trunk and longer branch lie east of the grid; only the shorter branch crosses into it
    path, flowdir = river_east
    trunk = [centre((10, 40)), centre((10, 70))]
    long_branch = [centre((-30, 40)), centre((10, 40))]
    short_branch = [centre((10, 20)), centre((10, 40))]
    y_reach = MultiLineString([trunk[::-1], long_branch[::-1], short_branch[::-1]])
    result = conflate(source([(1, -1, y_reach)]), flowdir)

    assert cells_of(result, 1) == path[20:]
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_flow_cycle_in_the_pointer_raster_does_not_hang(river_east, workspace_tmp):
    path, flowdir = river_east
    flowdir = flowdir.copy()
    flowdir[0, 0] = CODE[(0, 1)]
    flowdir[0, 1] = CODE[(0, -1)]
    result = conflate(source([(5, -1, line(path))]), flowdir)

    assert cells_of(result, 5) == path


def test_ids_too_large_for_the_stream_raster_raise(river_east):
    path, flowdir = river_east
    with pytest.raises(ValueError, match="32-bit"):
        conflate(source([(2**31, -1, line(path))]), flowdir)


def test_an_empty_source_network(river_east):
    path, flowdir = river_east
    empty = gpd.GeoDataFrame({"LINKNO": [], "DSLINKNO": [], "order": []}, geometry=[], crs="EPSG:32615")
    result = conflate(empty, flowdir)

    assert result.streams.empty
    assert not result.raster.any()


def test_a_reach_too_short_to_match_takes_its_cells_from_the_reaches_beside_it(river_east, workspace_tmp):
    path, flowdir = river_east
    src = source([(1, 2, line(path[:20])), (2, 3, line(path[19:21])), (3, -1, line(path[20:]))])
    result = conflate(src, flowdir)

    assert len(cells_of(result, 2)) == 2
    assert cells_of(result, 1) + cells_of(result, 2) + cells_of(result, 3) == path
    assert ds_of(result, 1) == 2 and ds_of(result, 2) == 3
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_large_tree_keeps_the_contract(workspace_tmp):
    # A trunk with eight side tributaries, each source line nudged off its channel
    rng = np.random.default_rng(3)
    trunk = straight(30, 0, 30, 79)
    paths, rows = [trunk], []
    breaks = list(range(0, 80, 10)) + [80]
    for k, (a, b) in enumerate(zip(breaks[:-1], breaks[1:]), start=1):
        rows.append((100 + k, 100 + k + 1 if b < 80 else -1, line(trunk[a:b + 1] if b < 80 else trunk[a:]), 2))
    for k, col in enumerate(range(5, 80, 10), start=1):
        side = 1 if k % 2 else -1
        trib = straight(30 + side * 12, col, 30 + side, col) + [(30, col)]
        paths.append(trib)
        reach = 100 + 1 + col // 10
        rows.append((k, reach, line(trib[:-1], offset=tuple(rng.uniform(-0.6, 0.6, 2))), 1))
    flowdir = flowdir_for((61, 80), paths, exits={(30, 79): (0, 1)})
    result = conflate(source(rows), flowdir)

    assert len(result.streams) == len(rows)
    assert_fldpln_contract(result, flowdir, workspace_tmp)


def test_a_flow_network_can_be_reused(river_east):
    path, flowdir = river_east
    flow = FlowNetwork(flowdir, GT, utm_wkt())
    first = conflate(source([(5, -1, line(path))]), flowdir, flow=flow)
    second = conflate(source([(5, -1, line(path))]), flowdir, flow=flow)

    assert np.array_equal(first.raster, second.raster)


def test_unmatched_below_counts_the_usable_line_past_the_match():
    from nencarta.tasks.stream_conflation import _unmatched_below
    offsets = np.array([0, 10, 16])
    usable = np.ones(16, bool)
    usable[7] = False   # outside the domain, say
    # Reach 0 was matched along its line up to sample 3; reach 1 against it, up to position 12 of its reversed
    # samples, which is sample 10 + 16 - 1 - 12 = 13
    unmatched = _unmatched_below(offsets, usable, np.array([3, 12]), np.array([False, True]))

    assert unmatched.tolist() == [5, 3]
    assert _unmatched_below(offsets, usable, np.array([-1, 15]), np.array([False, False])).tolist() == [0, 0]


@pytest.mark.parametrize("unmatched_m, related, end", [(0.0, True, 2), (500.0, True, 14), (500.0, False, 2)],
                         ids=["only max_extension_m", "as far as its own line ran on", "not into an unrelated reach"])
def test_a_reach_carries_on_to_the_reach_below_as_far_as_its_line_ran_on_unmatched(unmatched_m, related, end):
    """Reach 0 is matched on cells 0-2 of a straight D8 path, the reach it drains into on cells 15-20: 390 m
    further down, past max_extension_m (100 m). It reaches it when its own line ran on unmatched that far."""
    from nencarta.tasks.stream_conflation import _extend_ends
    n = 21
    down = np.r_[np.arange(1, n), -1].astype(np.int64)
    to_outlet = (n - 1 - np.arange(n)) * CELL
    ds = np.array([1 if related else -1, -1], np.int64)
    stin, stout = (np.array([1, 0]), np.array([1, 1])) if related else (np.array([0, 1]), np.array([0, 1]))
    claim_end = _extend_ends(np.array([0, 15], np.int64), np.array([2, 20], np.int64), down, np.arange(n), to_outlet,
                             np.zeros(n, np.bool_), ds, stin, stout, 100.0, CELL, np.array([unmatched_m, 0.0]))

    assert claim_end[0] == end
