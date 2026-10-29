"""
Move a source stream network onto a DEM's D8 flow paths.

ARC, Curve2Flood and FLDPLN follow streams along the DEM's flow directions, but a source network (TDX-Hydro,
GEOGLOWS, NHD, ...) was drawn from another DEM, so its lines drift on and off them. Each source reach is moved
onto the D8 path it follows, keeping its ID so the flows keyed by that ID still apply:

1. Each reach is sampled about once a cell, from upstream down, and matched onto the channel cells of the D8
   network as a hidden Markov model (map matching, Newson & Krumm 2009). The best-fitting stretch is kept. A
   reach too little of which follows the DEM is matched again, with a wider search and a looser fit.
2. Each reach meets the flow of its main upstream reach (see _reconnect). The matched reaches are then painted
   onto the D8 network in flow order, each from its matched start to where the next reach starts. Where two meet
   without a confluence in the source network, the source network decides which carries on, and otherwise the
   larger river does. A reach running beside a larger one before the two meet is drawn only to where it comes
   beside it (see _drawn_cells).
3. A reach left with a single cell takes a neighbouring one, and a reach left with none gets a short flow path of
   free cells near its line.

Each reach is matched on its own, so a reach the DEM disagrees with costs only that reach. Every reach with part
of its line on the DEM, outside lakes, is placed. Reaches split where the source network splits them, lakes can
cut the network, and every reach is one unbroken D8 path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from numba import njit
from osgeo import osr

from nencarta.logger import LOG

# D8 pointer codes, numbered as WhiteboxTools numbers them, mapped to the (row, col) offset of the cell each
# code drains into. Curve2Flood's FLDPLN spreader walks the stream info table with
# this same encoding, so the table has to be built against it.
D8_OFFSETS = {
    64: (-1, -1), 128: (-1, 0), 1: (-1, 1),
    32: (0, -1),                2: (0, 1),
    16: (1, -1),    8: (1, 0),  4: (1, 1),
}


def d8_offset_tables() -> tuple[np.ndarray, np.ndarray]:
    """Row/column offset per D8 code. An offset of (0, 0) means 'no outflow'."""
    row_offsets = np.zeros(256, np.int64)
    col_offsets = np.zeros(256, np.int64)
    for code, (d_row, d_col) in D8_OFFSETS.items():
        row_offsets[code] = d_row
        col_offsets[code] = d_col

    return row_offsets, col_offsets


@dataclass
class ConflationSettings:
    """
    How the source network is matched onto the D8 network.

    How far a source line can be from its river, and how far it can stray. The defaults
    suit the source streams burned into the DEM, where the D8 paths and the source lines rarely disagree by more
    than a cell or two.
    """
    # A cell is a channel cell, and so a candidate for a source line to match onto, when this much area drains
    # through it. Well below where any source network starts its streams, so that small source headwaters still
    # find a channel to match.
    min_channel_area_km2: float = 0.25
    # How far from a source sample a channel cell may be and still be a candidate (but at least
    # min_search_radius_cells), and how many of the nearest candidates are kept. The radius grows with the river
    # (see search_radius_per_sqrt_km2).
    search_radius_m: float = 150.0
    min_search_radius_cells: float = 3.0
    max_candidates: int = 16
    # Wide rivers' centrelines and thalwegs can be far apart: the radius grows by this many metres per square
    # root of drainage area (km2), up to max_search_radius_m. 1.0 gives ~300 m at 100 000 km2.
    search_radius_per_sqrt_km2: float = 1.0
    max_search_radius_m: float = 600.0
    # Sample spacing along a source line, in cells.
    sample_spacing_cells: float = 1.0
    # A sample's fit to a cell is 1 - (distance / sigma)^2 / 2: positive within ~1.4 sigma. sigma is this share of
    # the sample's search radius.
    sigma_per_radius: float = 0.3
    # A transition costs (|D8 path length - source line length| - slack) / beta between consecutive samples.
    beta_cells: float = 1.0
    slack_cells: float = 0.5
    # How long a stretch a match may skip in a row (where the source line strays off the channel), and what each
    # skipped sample costs on top of losing its fit.
    max_gap_m: float = 180.0
    gap_penalty: float = 0.25
    # A match must cover at least this many samples, and this fraction of the samples that could have been matched
    # (inside the domain and outside lakes), to be kept
    min_matched_samples: int = 3
    min_matched_fraction: float = 0.25
    # A reach that isn't kept -- most often a wide river whose flow path wanders across the flat water the DEM
    # shows, farther from its line than the search radius -- is matched again with a radius this many times as
    # wide, inside which any channel cell fits (the nearer the better), and kept however little of it matched.
    # One that still has no cells of its own gets a short flow path of free cells near its line (see
    # _stub_cells), so every reach with part of its line on the DEM, outside lakes, is placed.
    fallback_radius_factor: float = 3.0
    # A reach whose match ends short of the reach the source network sends it to may carry on down its D8 path to
    # reach it, for at most this far, or as far as its own line runs on below the match, unmatched, if that is
    # longer. Covers the gap left by a short reach that could not be matched, a tributary the DEM routes alongside
    # the main stem before the two meet (but see min_beside_cells), and the lower part of a reach whose line leaves
    # its river across braided or flat water, where the river's D8 path still leads to the reach below.
    max_extension_m: float = 3000.0
    # How much longer than that unmatched part of the line the D8 path may be: D8's staircase makes a path up to
    # ~8% longer than the line it follows, and across braided water the two wander differently.
    unmatched_extension_factor: float = 1.25
    # A reach whose last cells run beside another reach, a cell away, for at least this many cells before the two
    # meet ends at the first of them (see _drawn_cells): a small gap rather than two lines along one river.
    min_beside_cells: int = 3
    # How far down the D8 path from a reach's main upstream reach (the one bringing it at least half its flow)
    # _reconnect looks for where that flow goes: into the reach, which then starts where the upstream reach ends or
    # where that flow joins it, or past it into a reach further down, which moves the reach onto that path. Across
    # the flat water of braided and wide rivers D8 often takes another branch than the source network.
    max_reconnect_m: float = 10000.0
    # A reach whose match ends this close to some other matched reach (but at least min_snap_cells) is joined to
    # it, whatever the source network says about the two. And a reach draining into another this close to that
    # one's last cell drains where that one does, unless the source network sends it to that one.
    snap_m: float = 90.0
    min_snap_cells: float = 2.0
    # Reaches covering fewer cells than this are folded into the reach upstream of them.
    min_reach_cells: int = 2
    # A reach leaving a lake -- its first cell within outlet_lake_cells of one -- starts in the river below the
    # lake's outlet, not on the dam, weir or shore between the two (see _below_outlets): below its last step down of
    # at least outlet_drop_m from one cell to the next within max_outlet_m of its start (but at least
    # min_outlet_cells), as long as the river then falls less than that for outlet_tailwater_cells cells.
    outlet_lake_cells: int = 2
    outlet_drop_m: float = 1.0
    max_outlet_m: float = 450.0
    min_outlet_cells: int = 3
    outlet_tailwater_cells: int = 3


# ---------------------------------------------------------------------------------------------------------------
# The D8 network
# ---------------------------------------------------------------------------------------------------------------

def cell_sizes_m(geotransform: tuple, projection: str, nrows: int) -> tuple[np.ndarray, float]:
    """
    Width of a cell in metres on each row, and the height of a cell in metres.

    For a geographic CRS the width shrinks with the cosine of the latitude, so it is given per row; the height
    barely changes over a tile and is taken at its centre. Rotated grids are not supported.
    """
    if geotransform[2] != 0 or geotransform[4] != 0:
        raise ValueError(f"Rotated grids are not supported: geotransform {geotransform}.")
    
    srs = osr.SpatialReference()
    srs.ImportFromWkt(projection)
    if srs.IsGeographic():
        lat = np.radians(geotransform[3] + (np.arange(nrows) + 0.5) * geotransform[5])
        metres_per_degree_lat = 111132.92 - 559.82 * np.cos(2 * lat) + 1.175 * np.cos(4 * lat)
        metres_per_degree_lon = 111412.84 * np.cos(lat) - 93.5 * np.cos(3 * lat)
        dx = np.abs(geotransform[1]) * metres_per_degree_lon
        dy = float(np.abs(geotransform[5]) * metres_per_degree_lat[nrows // 2])
        return dx.astype(np.float64), dy
    
    metres_per_unit = srs.GetLinearUnits() if srs.IsProjected() else 1.0
    dx = np.full(nrows, abs(geotransform[1]) * metres_per_unit, np.float64)
    return dx, float(abs(geotransform[5]) * metres_per_unit)


@njit(cache=True)
def _downstream_cells(flowdir: np.ndarray, valid: np.ndarray, row_offsets: np.ndarray, col_offsets: np.ndarray,
                      down: np.ndarray) -> None:
    """
    Set ``down`` (flat, all -1) to the flat index of the cell each cell drains into, leaving -1 where its flow stops
    (a pit, nodata, the edge).
    """
    nrows, ncols = flowdir.shape
    for row in range(nrows):
        for col in range(ncols):
            if not valid[row, col]:
                continue

            code = flowdir[row, col]
            if code < 0 or code > 255:
                continue

            d_row = row_offsets[code]
            d_col = col_offsets[code]
            if d_row == 0 and d_col == 0:
                continue

            to_row = row + d_row
            to_col = col + d_col
            if to_row < 0 or to_row >= nrows or to_col < 0 or to_col >= ncols or not valid[to_row, to_col]:
                continue

            down[row * ncols + col] = to_row * ncols + to_col


@njit(cache=True)
def _accumulate(down: np.ndarray, cell_area_by_row: np.ndarray, ncols: int) -> tuple[np.ndarray, int]:
    """
    Drainage area of every cell, in flow order. Cells on a flow cycle, which a pointer raster made from a filled
    DEM never has, are cut loose from their downstream cell (in place) so that everything else stays a forest.
    Returns the areas and the number of cells cut.
    """
    n = down.size
    pending = np.zeros(n, np.int32)
    for i in range(n):
        j = down[i]
        if j >= 0:
            pending[j] += 1

    area = np.empty(n, np.float32)
    for i in range(n):
        area[i] = cell_area_by_row[i // ncols]

    queue = np.empty(n, down.dtype)
    head = 0
    tail = 0
    for i in range(n):
        if pending[i] == 0:
            queue[tail] = i
            tail += 1

    while head < tail:
        i = queue[head]
        head += 1
        j = down[i]
        if j >= 0:
            area[j] += area[i]
            pending[j] -= 1
            if pending[j] == 0:
                queue[tail] = j
                tail += 1

    cut = 0
    if tail < n:
        for i in range(n):
            if pending[i] > 0 and down[i] >= 0:
                down[i] = -1
                cut += 1

    return area, cut


@njit(cache=True)
def _euler_tour(parent: np.ndarray, step: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Number a forest given by each node's parent (-1 at a root) so that ``b`` is an ancestor of ``a``, or ``a``
    itself, exactly when ``tin[b] <= tin[a] <= tout[b]``, and give each node its summed ``step`` to its root.

    With parents pointing downstream that reads "``b`` is downstream of ``a``". Iterative: numba's cache cannot
    reload a recursive function. Nodes that never reach a root keep ``tin == -1``.
    """
    m = parent.size
    first_child = np.zeros(m + 1, np.int64)
    for k in range(m):
        p = parent[k]
        if p >= 0:
            first_child[p + 1] += 1

    for k in range(m):
        first_child[k + 1] += first_child[k]

    children = np.empty(max(first_child[m], 1), np.int64)
    fill = first_child[:m].copy()
    for k in range(m):
        p = parent[k]
        if p >= 0:
            children[fill[p]] = k
            fill[p] += 1

    tin = np.full(m, -1, np.int64)
    tout = np.full(m, -1, np.int64)
    to_root = np.zeros(m, np.float64)
    stack = np.empty(m, np.int64)
    next_child = np.empty(m, np.int64)
    clock = 0
    for root in range(m):
        if parent[root] >= 0:
            continue

        stack[0] = root
        next_child[root] = first_child[root]
        tin[root] = clock
        clock += 1
        top = 0
        while top >= 0:
            k = stack[top]
            if next_child[k] < first_child[k + 1]:
                child = children[next_child[k]]
                next_child[k] += 1
                to_root[child] = to_root[k] + step[child]
                tin[child] = clock
                clock += 1
                top += 1
                stack[top] = child
                next_child[child] = first_child[child]
            else:
                tout[k] = clock - 1
                top -= 1

    return tin, tout, to_root


@njit(cache=True)
def _step_lengths(cells: np.ndarray, down: np.ndarray, ncols: int, dx_m: np.ndarray, dy_m: float) -> np.ndarray:
    """Metres from each of ``cells`` to the cell it drains into (0 where its flow stops)."""
    out = np.zeros(cells.size, np.float64)
    for k in range(cells.size):
        i = cells[k]
        j = down[i]
        if j < 0:
            continue

        d_row = j // ncols - i // ncols
        d_col = j % ncols - i % ncols
        out[k] = math.sqrt((d_col * dx_m[i // ncols]) ** 2 + (d_row * dy_m) ** 2)

    return out


@njit(cache=True)
def _add_inflow(down: np.ndarray, area: np.ndarray, cells: np.ndarray, amounts: np.ndarray) -> None:
    """Add ``amounts[k]`` to the drainage area of ``cells[k]`` and of every cell downstream of it, in place."""
    for k in range(cells.size):
        cell = cells[k]
        steps = 0
        while cell >= 0 and steps < down.size:
            area[cell] += amounts[k]
            cell = down[cell]
            steps += 1


class FlowNetwork:
    """
    A D8 flow direction raster as a forest of cells, each pointing at the cell it drains into.

    After ``build_channels``, cells draining at least a threshold area are channel cells. They are closed
    downstream -- a channel cell drains into another channel cell -- so they form a forest of their own, which is
    given an Euler tour: whether one channel cell is downstream of another, and how far along the flow path, are
    then O(1).
    """

    def __init__(self, flowdir: np.ndarray, geotransform: tuple, projection: str, valid: np.ndarray | None = None):
        flowdir = np.ascontiguousarray(flowdir)
        if flowdir.dtype.kind not in "iu":
            flowdir = flowdir.astype(np.int64)

        self.nrows, self.ncols = flowdir.shape
        self.geotransform = tuple(geotransform)
        self.projection = projection
        if valid is None:
            valid = (flowdir >= 0) & (flowdir <= 255)

        self.valid = np.ascontiguousarray(valid, dtype=np.bool_)
        self.dx_m, self.dy_m = cell_sizes_m(self.geotransform, projection, self.nrows)
        self.cell_size_m = float(math.sqrt(float(np.median(self.dx_m)) * self.dy_m))

        row_offsets, col_offsets = d8_offset_tables()
        # Flat cell indices, int32 where they fit: the largest arrays the network holds take half the memory
        index_type = np.int32 if self.nrows * self.ncols < np.iinfo(np.int32).max else np.int64
        self.down = np.full(self.nrows * self.ncols, -1, index_type)
        _downstream_cells(flowdir, self.valid, row_offsets, col_offsets, self.down)
        self.area_km2, cut = _accumulate(self.down, self.dx_m * self.dy_m / 1e6, self.ncols)
        if cut:
            LOG.warning(f"The flow direction raster has {cut} cells on flow cycles; they were cut from the network.")

        self.channel_cells = np.empty(0, np.int64)

    @classmethod
    def without_flow(cls, shape: tuple[int, int], geotransform: tuple, projection: str,
                     valid: np.ndarray | None = None) -> FlowNetwork:
        """
        The network of a flow direction raster with no flow anywhere, each cell draining only itself, made without
        tracing it: for the grid, its cells' sizes and their drainage areas alone. It has no ``down``.
        """
        self = cls.__new__(cls)
        self.nrows, self.ncols = shape
        self.geotransform = tuple(geotransform)
        self.projection = projection
        self.valid = np.ones(shape, np.bool_) if valid is None else np.ascontiguousarray(valid, dtype=np.bool_)
        self.dx_m, self.dy_m = cell_sizes_m(self.geotransform, projection, self.nrows)
        self.cell_size_m = float(math.sqrt(float(np.median(self.dx_m)) * self.dy_m))
        self.down = None
        self.area_km2 = np.repeat((self.dx_m * self.dy_m / 1e6).astype(np.float32), self.ncols)
        self.channel_cells = np.empty(0, np.int64)
        return self

    def build_channels(self, min_channel_area_km2: float, inflow_cells: np.ndarray | None = None,
                       inflow_km2: np.ndarray | None = None) -> None:
        """
        Make the cells draining at least ``min_channel_area_km2`` the channel network.

        A river flowing in across the edge of the domain drains almost nothing within it where it enters, so the
        flow path from each of ``inflow_cells`` is counted as draining ``inflow_km2`` (default: the threshold
        area) on top of its own. The result is ``channel_area_km2``.
        """
        area = self.area_km2
        if inflow_cells is not None and len(inflow_cells):
            cells = np.asarray(inflow_cells, np.int64)
            amounts = np.full(cells.size, min_channel_area_km2, np.float32) if inflow_km2 is None else np.asarray(inflow_km2, np.float32)
            area = area.copy()
            _add_inflow(self.down, area, cells, amounts)

        self.channel_area_km2 = area
        self.channel_cells = np.flatnonzero((area >= min_channel_area_km2) & self.valid.ravel())
        # A raster's worth of ids: int32 when they fit, halving its memory
        id_type = np.int32 if self.channel_cells.size < np.iinfo(np.int32).max else np.int64
        self.channel_id = np.full(self.down.size, -1, id_type)
        self.channel_id[self.channel_cells] = np.arange(self.channel_cells.size, dtype=id_type)
        downstream = self.down[self.channel_cells]
        parent = np.where(downstream >= 0, self.channel_id[np.maximum(downstream, 0)], -1).astype(np.int64)
        step = _step_lengths(self.channel_cells, self.down, self.ncols, self.dx_m, self.dy_m)
        self.tin, self.tout, self.to_outlet_m = _euler_tour(parent, step)

    def cell_centres(self, cells: np.ndarray) -> np.ndarray:
        """Map coordinates of the centres of flat ``cells``, as an (n, 2) array."""
        gt = self.geotransform
        rows = cells // self.ncols + 0.5
        cols = cells % self.ncols + 0.5
        return np.column_stack((gt[0] + cols * gt[1], gt[3] + rows * gt[5]))


# ---------------------------------------------------------------------------------------------------------------
# The source network
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class SourceNetwork:
    """The source reaches, indexed 0..n-1, with their downstream reach and a numbering for ancestry tests."""
    ids: np.ndarray            # int64 source id of each reach
    ds: np.ndarray             # int64 index of the reach downstream of each reach, -1 for none
    geometry: np.ndarray       # shapely LineStrings, drawn upstream to downstream
    order: np.ndarray          # float64 stream order (0 where unknown)
    area: np.ndarray           # float64 drainage area in km2 (0 where unknown)
    tin: np.ndarray            # Euler tour of the network with parents downstream: a is b or downstream of it
    tout: np.ndarray           # exactly when tin[a] <= tin[b] <= tout[a]


def _line_parts(geom) -> list:
    """The non-empty LineStrings of a (merged) line geometry."""
    if geom is None or geom.is_empty:
        return []
    
    if geom.geom_type == "LineString":
        return [geom]
    
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "LineString" and not g.is_empty and g.length > 0]


def _main_stem(parts: list, outlet_score) -> shapely.LineString:
    """
    One line through a reach drawn in several pieces: from its outlet up the longest way through the pieces,
    drawn from upstream down.

    Such a reach is usually Y-shaped: two headwater branches and the trunk below where they meet (TDX-Hydro folds
    first-order streams into the reach below). The trunk and its longer branch are matched, since the other branch
    can't be on the same D8 path. Pieces join where their ends touch; the end ``outlet_score`` ranks highest is
    the outlet.
    """
    tolerance = 1e-6 * sum(p.length for p in parts)
    ends = [(np.asarray(p.coords[0][:2]), np.asarray(p.coords[-1][:2])) for p in parts]
    nodes = []
    part_nodes = []
    for a, b in ends:
        found = []
        for xy in (a, b):
            for k, node in enumerate(nodes):
                if np.hypot(*(node - xy)) <= tolerance:
                    found.append(k)
                    break
            else:
                nodes.append(xy)
                found.append(len(nodes) - 1)

        part_nodes.append(tuple(found))

    scores = [outlet_score(shapely.Point(xy)) for xy in nodes]
    outlet = int(np.argmax(scores))

    # The farthest node from the outlet through the pieces, and the pieces on the way there
    adjacent = [[] for _ in nodes]
    for i, (a, b) in enumerate(part_nodes):
        adjacent[a].append((i, b))
        adjacent[b].append((i, a))

    reach = {outlet: (0.0, [])}
    stack = [outlet]
    while stack:
        node = stack.pop()
        length, path = reach[node]
        for i, other in adjacent[node]:
            if other not in reach:
                reach[other] = (length + parts[i].length, path + [(i, node)])
                stack.append(other)

    far = max(reach, key=lambda k: reach[k][0])
    path = reach[far][1]
    if not path:  # the outlet's piece touches no other: take the longest piece on its own
        part = max(range(len(parts)), key=lambda i: (outlet in part_nodes[i], parts[i].length))
        coords = np.asarray(parts[part].coords)[:, :2]
        return shapely.LineString(coords if part_nodes[part][1] == outlet else coords[::-1])

    # Walk back from the far end to the outlet, each piece turned to run that way
    coords = []
    for i, downstream_node in reversed(path):
        piece = np.asarray(parts[i].coords)[:, :2]
        if part_nodes[i][1] != downstream_node:
            piece = piece[::-1]

        coords.extend(piece if not coords else piece[1:])

    return shapely.LineString(coords)


def _break_cycles(ds: np.ndarray) -> int:
    """Point the reach that closes each cycle of ``ds`` at nothing. Returns how many cycles were broken."""
    n = ds.size
    state = np.zeros(n, np.int8)  # 0 unseen, 1 on the current walk, 2 done
    broken = 0
    for start in range(n):
        walk = []
        node = start
        while node >= 0 and state[node] == 0:
            state[node] = 1
            walk.append(node)
            node = ds[node]

        if node >= 0 and state[node] == 1:
            ds[walk[-1]] = -1
            broken += 1

        for visited in walk:
            state[visited] = 2

    return broken


def prepare_source_network(source_gdf: gpd.GeoDataFrame, id_col: str, ds_col: str, order_col: str | None,
                           area_col: str | None, flow: FlowNetwork) -> SourceNetwork:
    """
    Clean the source reaches into a forest whose lines run downstream.

    Reaches without a usable line or with an id that can't label a raster cell (missing, or not above 0) are
    dropped; a reach listed twice keeps its first row; a downstream id that isn't a reach here (outside the
    domain, or 0/-1 for "none") becomes "no downstream"; a reach pointing at itself or closing a cycle is cut.

    Source networks draw their lines either way (TDX-Hydro and GEOGLOWS draw them downstream to upstream), so
    each line is turned to run downstream. A line's downstream end is the one nearer the reach it drains into;
    without one, the end farther from the reaches draining into it; and for a reach with neither, the end with
    more area draining through the DEM there.
    """
    gdf = source_gdf[[c for c in dict.fromkeys([id_col, ds_col, order_col, area_col, source_gdf.geometry.name]) if c]]
    ids = pd.to_numeric(gdf[id_col], errors="coerce")
    geoms = gdf.geometry.to_numpy()
    usable = (ids.notna() & (ids > 0)).to_numpy() & ~shapely.is_missing(geoms) & ~shapely.is_empty(geoms)
    gdf = gdf[usable]
    gdf = gdf[~pd.to_numeric(gdf[id_col]).duplicated().to_numpy()]

    merged = shapely.line_merge(shapely.force_2d(gdf.geometry.to_numpy()))
    parts = [_line_parts(g) for g in merged]
    keep = np.array([len(p) > 0 for p in parts], bool)
    gdf, merged = gdf[keep], merged[keep]
    parts = [p for p, k in zip(parts, keep) if k]

    ids = pd.to_numeric(gdf[id_col]).to_numpy(np.int64)
    position = {rid: i for i, rid in enumerate(ids)}
    ds_ids = pd.to_numeric(gdf[ds_col], errors="coerce").to_numpy()
    ds = np.array([position.get(int(d), -1) if np.isfinite(d) else -1 for d in ds_ids], np.int64)
    ds[ds == np.arange(ds.size)] = -1
    broken = _break_cycles(ds)
    if broken:
        LOG.warning(f"The source network has {broken} cycles; each was cut at one reach.")

    # A reach whose line can't be merged into one is matched along its main stem (see _main_stem). Its outlet is
    # the end nearest the reach it drains into; without one, the end farthest from the reaches draining into it;
    # and with neither, the end the most drains through on the DEM. A main stem wholly off the grid, where only a
    # branch crosses into it, is taken from the pieces on the grid instead.
    upstream = [[] for _ in range(ids.size)]
    for i in np.flatnonzero(ds >= 0):
        upstream[ds[i]].append(i)

    gt = flow.geotransform
    grid = shapely.box(gt[0], gt[3] + flow.nrows * gt[5], gt[0] + flow.ncols * gt[1], gt[3])
    lines = np.empty(ids.size, dtype=object)
    for i, pieces in enumerate(parts):
        if len(pieces) == 1:
            lines[i] = pieces[0]
            continue

        if ds[i] >= 0:
            target = merged[ds[i]]
            outlet_score = lambda point, target=target: -point.distance(target)
        elif upstream[i]:
            sources = shapely.union_all(merged[upstream[i]])
            outlet_score = lambda point, sources=sources: point.distance(sources)
        else:
            outlet_score = lambda point: _area_near(flow, point)
            
        lines[i] = _main_stem(pieces, outlet_score)
        if not lines[i].intersects(grid):
            on_grid = [part for piece in pieces for part in _line_parts(piece.intersection(grid))]
            if len(on_grid) == 1:
                lines[i] = on_grid[0]
            elif on_grid:
                lines[i] = _main_stem(on_grid, outlet_score)

    order = pd.to_numeric(gdf[order_col], errors="coerce").fillna(0).to_numpy(np.float64) \
        if order_col and order_col in gdf.columns else np.zeros(ids.size)
    area = pd.to_numeric(gdf[area_col], errors="coerce").fillna(0).to_numpy(np.float64) \
        if area_col and area_col in gdf.columns else np.zeros(ids.size)

    lines = _orient_downstream(lines, ds, flow)
    tin, tout, _ = _euler_tour(ds, np.zeros(ds.size))
    return SourceNetwork(ids, ds, lines, order, area, tin, tout)


def _orient_downstream(lines: np.ndarray, ds: np.ndarray, flow: FlowNetwork) -> np.ndarray:
    n = lines.size
    first = shapely.points([g.coords[0] for g in lines]) if n else np.array([], dtype=object)
    last = shapely.points([g.coords[-1] for g in lines]) if n else np.array([], dtype=object)
    flip = np.zeros(n, bool)
    decided = np.zeros(n, bool)

    has_ds = ds >= 0
    if has_ds.any():
        target = lines[ds[has_ds]]
        d_first = shapely.distance(first[has_ds], target)
        d_last = shapely.distance(last[has_ds], target)
        idx = np.flatnonzero(has_ds)
        flip[idx] = d_first < d_last
        decided[idx] = d_first != d_last

    upstream = [[] for _ in range(n)]
    for i in np.flatnonzero(has_ds):
        upstream[ds[i]].append(i)

    for i in np.flatnonzero(~decided):
        if not upstream[i]:
            continue

        ups = shapely.union_all(lines[upstream[i]])
        d_first = shapely.distance(first[i], ups)
        d_last = shapely.distance(last[i], ups)
        if d_first != d_last:
            flip[i] = d_last < d_first
            decided[i] = True

    # A reach with neither: the end where more drains through the DEM. Taken at the line's last points on the DEM,
    # about a cell apart, since an end can lie off the grid or out at sea.
    for i in np.flatnonzero(~decided):
        points = shapely.get_coordinates(shapely.segmentize(lines[i], abs(flow.geotransform[1])))
        on_dem = [p for p in shapely.points(points) if _on_valid_cell(flow, p)]
        if len(on_dem) >= 2:
            flip[i] = _area_near(flow, on_dem[0]) > _area_near(flow, on_dem[-1])

    out = lines.copy()
    for i in np.flatnonzero(flip):
        out[i] = shapely.reverse(lines[i])

    return out


def _on_valid_cell(flow: FlowNetwork, point) -> bool:
    gt = flow.geotransform
    col = math.floor((point.x - gt[0]) / gt[1])
    row = math.floor((point.y - gt[3]) / gt[5])
    return 0 <= row < flow.nrows and 0 <= col < flow.ncols and bool(flow.valid[row, col])


def _area_near(flow: FlowNetwork, point, radius_cells: int = 2) -> float:
    """The largest drainage area on the DEM within a couple of cells of ``point``, or 0 if there is none."""
    gt = flow.geotransform
    col = math.floor((point.x - gt[0]) / gt[1])
    row = math.floor((point.y - gt[3]) / gt[5])
    r0, r1 = max(row - radius_cells, 0), min(row + radius_cells + 1, flow.nrows)
    c0, c1 = max(col - radius_cells, 0), min(col + radius_cells + 1, flow.ncols)
    if r0 >= r1 or c0 >= c1:
        return 0.0
    
    window = flow.area_km2.reshape(flow.nrows, flow.ncols)[r0:r1, c0:c1]
    valid = flow.valid[r0:r1, c0:c1]
    return float(window[valid].max()) if valid.any() else 0.0


# ---------------------------------------------------------------------------------------------------------------
# Matching each reach
# ---------------------------------------------------------------------------------------------------------------

@njit(cache=True)
def _resample(xs: np.ndarray, ys: np.ndarray, offsets: np.ndarray, spacing: float
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Points every ``spacing`` along each polyline (vertices ``offsets[i]:offsets[i+1]``, in metre-like
    coordinates), always including both ends. Returns the points, the distance of each along its line, and the
    offsets of each line's points.
    """
    n_lines = offsets.size - 1
    total = 0
    for i in range(n_lines):
        length = 0.0
        for v in range(offsets[i] + 1, offsets[i + 1]):
            length += math.hypot(xs[v] - xs[v - 1], ys[v] - ys[v - 1])

        total += math.floor(length / spacing) + 2

    out_x = np.empty(total, np.float64)
    out_y = np.empty(total, np.float64)
    along = np.empty(total, np.float64)
    out_offsets = np.zeros(n_lines + 1, np.int64)
    k = 0
    for i in range(n_lines):
        start, stop = offsets[i], offsets[i + 1]
        out_x[k] = xs[start]
        out_y[k] = ys[start]
        along[k] = 0.0
        k += 1
        travelled = 0.0
        next_at = spacing
        for v in range(start + 1, stop):
            seg = math.hypot(xs[v] - xs[v - 1], ys[v] - ys[v - 1])
            while seg > 0 and next_at <= travelled + seg - 1e-9:
                t = (next_at - travelled) / seg
                out_x[k] = xs[v - 1] + t * (xs[v] - xs[v - 1])
                out_y[k] = ys[v - 1] + t * (ys[v] - ys[v - 1])
                along[k] = next_at
                k += 1
                next_at += spacing

            travelled += seg

        if travelled > along[k - 1] + 1e-9 or k - out_offsets[i] == 1:
            out_x[k] = xs[stop - 1]
            out_y[k] = ys[stop - 1]
            along[k] = travelled
            k += 1

        out_offsets[i + 1] = k

    return out_x[:k], out_y[:k], along[:k], out_offsets


@njit(cache=True)
def _find_candidates(rows_f: np.ndarray, cols_f: np.ndarray, usable: np.ndarray, radius_m: np.ndarray,
                     k_max: int, channel_id: np.ndarray, excluded: np.ndarray, nrows: int, ncols: int,
                     dx_m: np.ndarray, dy_m: float, area: np.ndarray, min_area: np.ndarray,
                     max_area: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    The ``k_max`` nearest channel cells (as channel ids) within ``radius_m`` of each usable sample, of those whose
    ``area`` is within the sample's ``min_area`` and ``max_area``.
    """
    n = rows_f.size
    cand = np.full((n, k_max), -1, np.int64)
    cand_dist = np.full((n, k_max), np.inf, np.float64)
    count = np.zeros(n, np.int64)
    for s in range(n):
        if not usable[s]:
            continue

        rf = rows_f[s]
        cf = cols_f[s]
        r_mid = min(max(math.floor(rf), 0), nrows - 1)
        c_mid = math.floor(cf)
        reach_r = math.ceil(radius_m[s] / dy_m)
        reach_c = math.ceil(radius_m[s] / dx_m[r_mid])
        for r in range(max(r_mid - reach_r, 0), min(r_mid + reach_r + 1, nrows)):
            for c in range(max(c_mid - reach_c, 0), min(c_mid + reach_c + 1, ncols)):
                cell = r * ncols + c
                k = channel_id[cell]
                if k < 0 or excluded[cell] or area[cell] < min_area[s] or area[cell] > max_area[s]:
                    continue

                d = math.sqrt(((c + 0.5 - cf) * dx_m[r]) ** 2 + ((r + 0.5 - rf) * dy_m) ** 2)
                if d > radius_m[s]:
                    continue

                m = count[s]
                if m < k_max:
                    pos = m
                    count[s] = m + 1
                elif d < cand_dist[s, k_max - 1]:
                    pos = k_max - 1
                else:
                    continue

                while pos > 0 and cand_dist[s, pos - 1] > d:
                    cand[s, pos] = cand[s, pos - 1]
                    cand_dist[s, pos] = cand_dist[s, pos - 1]
                    pos -= 1

                cand[s, pos] = k
                cand_dist[s, pos] = d

    return cand, cand_dist, count


@njit(cache=True)
def _align_run(a: int, b: int, along: np.ndarray, cand: np.ndarray, cand_dist: np.ndarray, count: np.ndarray,
               tin: np.ndarray, tout: np.ndarray, to_outlet: np.ndarray, sigma: np.ndarray, beta: float,
               slack: float, max_gap: int, gap_penalty: float) -> tuple[float, int, int, int, int, int]:
    """
    The best local alignment of samples ``a..b-1`` onto one D8 path (Viterbi with free start and end).

    A state is a candidate channel cell of a sample. State ``k`` of sample ``i`` may follow state ``q`` of an
    earlier sample ``p`` (at most ``max_gap`` samples back) only when ``k`` is ``q`` or downstream of it, and then
    costs how far the D8 path from ``q`` to ``k`` differs in length from the source line between the two samples.
    A sample's fit is ``1 - (distance / sigma)^2 / 2``.

    Returns (score, first sample, last sample, first channel cell, last channel cell, matched samples).
    """
    n = b - a
    k_max = cand.shape[1]
    score = np.full((max(n, 1), k_max), -np.inf)
    back = np.full((max(n, 1), k_max), -1, np.int64)
    best = 0.0
    best_state = -1
    for ii in range(n):
        i = a + ii
        for j in range(count[i]):
            kj = cand[i, j]
            fit = 1.0 - 0.5 * (cand_dist[i, j] / sigma[i]) ** 2
            top = 0.0
            arg = -1
            for g in range(1, max_gap + 1):
                pp = ii - g
                if pp < 0:
                    break

                p = a + pp
                expected = along[i] - along[p]
                for q in range(count[p]):
                    sq = score[pp, q]
                    if sq <= 0.0:
                        continue

                    kq = cand[p, q]
                    if not (tin[kj] <= tin[kq] and tin[kq] <= tout[kj]):
                        continue

                    excess = abs(to_outlet[kq] - to_outlet[kj] - expected) - slack
                    excess = max(excess, 0.0)
                    s = sq - excess / beta - (g - 1) * gap_penalty
                    if s > top:
                        top = s
                        arg = pp * k_max + q

            score[ii, j] = top + fit
            back[ii, j] = arg
            if score[ii, j] > best:
                best = score[ii, j]
                best_state = ii * k_max + j

    if best_state < 0:
        return 0.0, -1, -1, -1, -1, 0
    
    last_sample = best_state // k_max
    last_cell = cand[a + last_sample, best_state % k_max]
    state = best_state
    matched = 0
    first_sample = last_sample
    first_cell = last_cell
    while state >= 0:
        ii = state // k_max
        matched += 1
        first_sample = ii
        first_cell = cand[a + ii, state % k_max]
        state = back[ii, state % k_max]

    return best, a + first_sample, a + last_sample, first_cell, last_cell, matched


@njit(cache=True)
def _align_reaches(sample_offsets: np.ndarray, usable: np.ndarray, along: np.ndarray, cand: np.ndarray,
                   cand_dist: np.ndarray, count: np.ndarray, tin: np.ndarray, tout: np.ndarray,
                   to_outlet: np.ndarray, sigma: np.ndarray, beta: float, slack: float, max_gap: int,
                   gap_penalty: float):
    """Align every reach: each run of usable samples separately, keeping the best-scoring run."""
    n_reaches = sample_offsets.size - 1
    score = np.zeros(n_reaches)
    first_sample = np.full(n_reaches, -1, np.int64)
    last_sample = np.full(n_reaches, -1, np.int64)
    first_cell = np.full(n_reaches, -1, np.int64)
    last_cell = np.full(n_reaches, -1, np.int64)
    matched = np.zeros(n_reaches, np.int64)
    for r in range(n_reaches):
        s = sample_offsets[r]
        stop = sample_offsets[r + 1]
        while s < stop:
            while s < stop and not usable[s]:
                s += 1

            run_start = s
            while s < stop and usable[s]:
                s += 1

            if s == run_start:
                continue

            res = _align_run(run_start, s, along, cand, cand_dist, count, tin, tout, to_outlet, sigma, beta,
                             slack, max_gap, gap_penalty)
            if res[0] > score[r]:
                score[r] = res[0]
                first_sample[r] = res[1]
                last_sample[r] = res[2]
                first_cell[r] = res[3]
                last_cell[r] = res[4]
                matched[r] = res[5]

    return score, first_sample, last_sample, first_cell, last_cell, matched


# ---------------------------------------------------------------------------------------------------------------
# Painting the matched reaches onto the D8 network
# ---------------------------------------------------------------------------------------------------------------

@njit(cache=True)
def _is_downstream(tin: np.ndarray, tout: np.ndarray, a: int, b: int) -> bool:
    """Whether node ``a`` is ``b`` or downstream of it, in a tour whose parents point downstream."""
    return tin[a] <= tin[b] and tin[b] <= tout[a]


@njit(cache=True)
def _related(r: int, x: int, ds: np.ndarray, stin: np.ndarray, stout: np.ndarray) -> bool:
    """
    Whether the source network sends reach ``r`` on to reach ``x``: x is downstream of r, or shares r's
    downstream reach (two tributaries of one river, or a tributary and a reach of that river).
    """
    if _is_downstream(stin, stout, x, r):
        return True
    return ds[r] >= 0 and ds[x] == ds[r]


@njit(cache=True)
def _matched_cells(start: np.ndarray, end: np.ndarray, down: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Which reaches matched onto each cell, as a (cell, reach) list sorted by cell."""
    total = 0
    for r in range(start.size):
        if start[r] < 0:
            continue

        cell = start[r]
        while True:
            total += 1
            if cell == end[r] or down[cell] < 0:
                break

            cell = down[cell]

    owner_cell = np.empty(total, np.int64)
    owner = np.empty(total, np.int64)
    k = 0
    for r in range(start.size):
        if start[r] < 0:
            continue

        cell = start[r]
        while True:
            owner_cell[k] = cell
            owner[k] = r
            k += 1
            if cell == end[r] or down[cell] < 0:
                break

            cell = down[cell]

    order = np.argsort(owner_cell, kind="mergesort")
    return owner_cell[order], owner[order]


@njit(cache=True)
def _reconnect(start: np.ndarray, end: np.ndarray, feeder: np.ndarray, down: np.ndarray, channel_id: np.ndarray,
               ctin: np.ndarray, ctout: np.ndarray, to_outlet: np.ndarray, stop_mask: np.ndarray, ds: np.ndarray,
               stin: np.ndarray, stout: np.ndarray, max_extension_m: float, max_reconnect_m: float,
               max_steps: int) -> int:
    """
    Follow each reach's main upstream reach (``feeder``, -1 for none) down its D8 path from its matched end, for
    at most ``max_reconnect_m``, to where its flow goes, and change the reach's match (in place) to meet it:

    - Into the reach's own match, but farther than _extend_ends carries the upstream reach (``max_extension_m``),
      or past another reach's match: the reach starts where the upstream reach ends.
    - Into the reach's own match, which starts on another path, a smaller tributary's or a hillside's, that the
      river's water doesn't take: the reach starts where the upstream reach's path joins it. A river started up a
      tributary floods the tributary's valley as deep as the river.
    - Into a reach the source network puts downstream of the reach, before the reach: the flow bypasses it, so the
      reach is moved onto the path between the two.

    Stops at ``stop_mask`` (lakes), and after ``max_steps`` cells. Returns how many reaches changed.
    """
    owner_cell, owner = _matched_cells(start, end, down)
    total = owner_cell.size
    path = np.empty(max_steps, np.int64)
    changed = 0
    for r in range(start.size):
        u = feeder[r]
        if u < 0 or start[u] < 0:
            continue

        base = to_outlet[channel_id[end[u]]]
        cell = down[end[u]]
        k = 0
        crossed = False   # the path ran through another reach's match on the way
        found = 0         # 1: reaches r, 2: reaches a reach below r first
        while cell >= 0 and not stop_mask[cell] and k < path.size:
            travelled = base - to_outlet[channel_id[cell]]
            if travelled > max_reconnect_m:
                break

            lo = np.searchsorted(owner_cell, cell)
            other = False
            while lo < total and owner_cell[lo] == cell:
                x = owner[lo]
                lo += 1
                if x == r:
                    found = 1
                elif x != u and _is_downstream(stin, stout, x, r):
                    if found == 0:
                        found = 2
                elif x != u:
                    other = True

            if found:
                break

            crossed = crossed or other
            path[k] = cell
            k += 1
            cell = down[cell]

        if found == 1 and k > 0 and (crossed or base - to_outlet[channel_id[path[k - 1]]] > max_extension_m):
            start[r] = path[0]
            changed += 1
        elif found == 1:
            # The first cell of the reach's match on the upstream reach's path: where the path enters it, or joins
            # it above the upstream reach's matched end
            first = start[r]
            while first != cell and not _is_downstream(ctin, ctout, channel_id[first], channel_id[start[u]]):
                first = down[first]

            if first != start[r]:
                start[r] = first
                changed += 1
        elif found == 2 and k >= 2:
            start[r] = path[0]
            end[r] = path[k - 1]
            changed += 1

    return changed


@njit(cache=True)
def _extend_ends(start: np.ndarray, end: np.ndarray, down: np.ndarray, channel_id: np.ndarray,
                 to_outlet: np.ndarray, stop_mask: np.ndarray, ds: np.ndarray, stin: np.ndarray,
                 stout: np.ndarray, max_extension_m: float, snap_m: float, unmatched_m: np.ndarray) -> np.ndarray:
    """
    Where each matched reach's claim ends.

    A reach claims the D8 path from its matched start to its matched end, cut short at the first cell of
    ``stop_mask`` (a lake). Past its matched end it carries on down the D8 path, while that stays clear of
    ``stop_mask``, until it reaches a cell another reach matched onto: when the source network sends the reach on
    to that reach (see _related) it may go as far as ``max_extension_m``, or as far as its own line ran on
    unmatched below its match (``unmatched_m``) if that is longer, to get there, and otherwise only ``snap_m``. A
    reach that reaches nobody ends where its match ended.
    """
    n = start.size
    owner_cell, owner = _matched_cells(start, end, down)
    total = owner_cell.size

    claim_end = np.full(n, -1, np.int64)
    for r in range(n):
        if start[r] < 0:
            continue

        # The matched path, cut at the first stop cell
        cell = start[r]
        if stop_mask[cell]:
            continue

        last = cell
        while cell != end[r]:
            nxt = down[cell]
            if nxt < 0 or stop_mask[nxt]:
                break

            cell = nxt
            last = cell

        claim_end[r] = last
        if last != end[r]:
            continue

        # Past the matched end
        base = to_outlet[channel_id[last]]
        reach_m = max(max_extension_m, unmatched_m[r])
        prev = last
        cell = down[last]
        while cell >= 0 and not stop_mask[cell]:
            travelled = base - to_outlet[channel_id[cell]]
            if travelled > reach_m:
                break

            lo = np.searchsorted(owner_cell, cell)
            hit = False
            joined = False
            while lo < total and owner_cell[lo] == cell:
                x = owner[lo]
                lo += 1
                if x == r:
                    continue

                hit = True
                if _related(r, x, ds, stin, stout) or travelled <= snap_m:
                    joined = True

            if hit:
                if joined:
                    claim_end[r] = prev
                break

            prev = cell
            cell = down[cell]

    return claim_end


@njit(cache=True)
def _outranks(a: int, b: int, order: np.ndarray, area: np.ndarray, score: np.ndarray) -> bool:
    if order[a] != order[b]:
        return order[a] > order[b]
    if area[a] != area[b]:
        return area[a] > area[b]
    if score[a] != score[b]:
        return score[a] > score[b]
    return a < b


@njit(cache=True)
def _paint(start: np.ndarray, claim_end: np.ndarray, down: np.ndarray, channel_id: np.ndarray,
           ctin: np.ndarray, ctout: np.ndarray, to_outlet: np.ndarray, ds: np.ndarray, stin: np.ndarray,
           stout: np.ndarray, order: np.ndarray, area: np.ndarray, score: np.ndarray, max_merge_m: float):
    """
    Label the claimed D8 paths with reaches, in flow order. See the module docstring for the rules.

    Returns the claimed cells (sorted), the reach on each (-1 for none), and each reach's first and last cell
    (-1 if it never won a cell).
    """
    n = start.size
    # The union of the claims. _extend_ends found each claim's end by walking this same path, so the walk
    # reaches it; the outlet check only guards against a corrupt pointer.
    total = 0
    for r in range(n):
        if claim_end[r] < 0:
            continue

        cell = start[r]
        while True:
            total += 1
            if cell == claim_end[r] or down[cell] < 0:
                break
            cell = down[cell]

    cells = np.empty(total, np.int64)
    k = 0
    for r in range(n):
        if claim_end[r] < 0:
            continue

        cell = start[r]
        while True:
            cells[k] = cell
            k += 1
            if cell == claim_end[r] or down[cell] < 0:
                break
            cell = down[cell]

    cells = np.unique(cells)
    m = cells.size

    # Upstream neighbours within the union, and the reaches starting at each cell
    downstream_index = np.full(m, -1, np.int64)
    pending = np.zeros(m, np.int64)
    for u in range(m):
        d = down[cells[u]]
        if d >= 0:
            j = np.searchsorted(cells, d)
            if j < m and cells[j] == d:
                downstream_index[u] = j
                pending[j] += 1

    first_up = np.zeros(m + 1, np.int64)
    for u in range(m):
        if downstream_index[u] >= 0:
            first_up[downstream_index[u] + 1] += 1

    for u in range(m):
        first_up[u + 1] += first_up[u]

    ups = np.empty(max(first_up[m], 1), np.int64)
    fill = first_up[:m].copy()
    for u in range(m):
        j = downstream_index[u]
        if j >= 0:
            ups[fill[j]] = u
            fill[j] += 1

    starters = np.empty(n, np.int64)
    starter_cell = np.empty(n, np.int64)
    n_starters = 0
    for r in range(n):
        if claim_end[r] >= 0:
            starters[n_starters] = r
            starter_cell[n_starters] = np.searchsorted(cells, start[r])
            n_starters += 1

    by_cell = np.argsort(starter_cell[:n_starters], kind="mergesort")
    starters = starters[:n_starters][by_cell]
    starter_cell = starter_cell[:n_starters][by_cell]

    label = np.full(m, -1, np.int64)
    first = np.full(n, -1, np.int64)
    last = np.full(n, -1, np.int64)
    alive = np.zeros(n, np.bool_)
    started = np.zeros(n, np.bool_)

    queue = np.empty(m, np.int64)
    head = 0
    tail = 0
    for u in range(m):
        if pending[u] == 0:
            queue[tail] = u
            tail += 1

    candidates = np.empty(n + 8, np.int64)
    while head < tail:
        u = queue[head]
        head += 1
        cell = cells[u]
        c = 0
        # Reaches flowing in from upstream that have not reached the end of their claim
        for t in range(first_up[u], first_up[u + 1]):
            v = ups[t]
            r = label[v]
            if r >= 0 and alive[r] and last[r] == cells[v] and cells[v] != claim_end[r]:
                candidates[c] = r
                c += 1

        # Reaches whose match starts here
        lo = np.searchsorted(starter_cell, u)
        while lo < n_starters and starter_cell[lo] == u:
            r = starters[lo]
            lo += 1
            if not started[r]:
                candidates[c] = r
                c += 1

        winner = -1
        if c == 1:
            winner = candidates[0]
        elif c > 1:
            # 1. A reach downstream of all the others in the source network carries on
            for i in range(c):
                x = candidates[i]
                ok = True
                for j in range(c):
                    if not _is_downstream(stin, stout, x, candidates[j]):
                        ok = False
                        break

                if ok:
                    winner = x
                    break

            # 2. Reaches that all drain into one source reach start it here, if its match lies further down
            #    this path
            if winner < 0:
                d = ds[candidates[0]]
                same = d >= 0
                for i in range(1, c):
                    if ds[candidates[i]] != d:
                        same = False
                        break
                    
                if same and claim_end[d] >= 0 and not started[d] and channel_id[cell] >= 0 \
                        and channel_id[start[d]] >= 0:
                    cd = channel_id[start[d]]
                    cc = channel_id[cell]
                    if _is_downstream(ctin, ctout, cd, cc) and to_outlet[cc] - to_outlet[cd] <= max_merge_m:
                        # Every cell between here and its match must be claimed, or it would be cut in two
                        walk = cell
                        covered = True
                        while walk != start[d]:
                            walk = down[walk]
                            if walk < 0:
                                covered = False
                                break
                            
                            j = np.searchsorted(cells, walk)
                            if j >= m or cells[j] != walk:
                                covered = False
                                break

                        if covered:
                            winner = d

            # 3. Otherwise the larger river carries on
            if winner < 0:
                winner = candidates[0]
                for i in range(1, c):
                    if _outranks(candidates[i], winner, order, area, score):
                        winner = candidates[i]

            for i in range(c):
                if candidates[i] != winner:
                    alive[candidates[i]] = False

        if winner >= 0:
            label[u] = winner
            if not started[winner]:
                started[winner] = True
                alive[winner] = True
                first[winner] = cell
            last[winner] = cell

        j = downstream_index[u]
        if j >= 0:
            pending[j] -= 1
            if pending[j] == 0:
                queue[tail] = j
                tail += 1

    return cells, label, first, last


# ---------------------------------------------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class ConflationResult:
    """The conflated network: one row per reach, and the stream raster it paints."""
    streams: gpd.GeoDataFrame
    raster: np.ndarray          # int32, the source id of each stream cell, 0 elsewhere
    matched_fraction: dict      # source id -> fraction of its usable length that was matched


def conflate_to_flow_directions(
        source_gdf: gpd.GeoDataFrame,
        flowdir: np.ndarray,
        geotransform: tuple,
        projection: str,
        id_col: str,
        ds_col: str,
        order_col: str | None = None,
        area_col: str | None = None,
        lake_mask: np.ndarray | None = None,
        valid_mask: np.ndarray | None = None,
        settings: ConflationSettings | None = None,
        flow: FlowNetwork | None = None,
        dem: np.ndarray | None = None,
) -> ConflationResult:
    """
    Move ``source_gdf``'s reaches onto the D8 network of ``flowdir`` (D8 pointer codes, see D8_OFFSETS).

    ``source_gdf`` must be in the raster's CRS. Its reaches keep their ids and every other column; each reach's
    line becomes the centres of the D8 cells it was matched onto, from upstream to downstream, and its downstream
    id becomes the reach its last cell drains into (-1 where that is no reach), or where that reach drains when it
    is entered at its last few cells and the source network doesn't send the reach there (see _reach_lines).
    Every reach with part of its line on the DEM, outside lakes, is placed; only reaches with none are left out.

    ``lake_mask`` (True inside a lake) cuts the network at lakes: no reach is matched inside one, and a reach
    flowing into one ends at its shore. With ``dem``, the filled DEM ``flowdir`` was derived from, a reach flowing
    out of one starts in the river below its outlet (see _below_outlets), and a river flowing in across the edge
    of the domain comes in at the river rather than on its bank (see _river_at_edge). ``valid_mask`` (False on
    nodata) stops flow there.
    """
    settings = settings or ConflationSettings()
    if flow is None:
        flow = FlowNetwork(flowdir, geotransform, projection, valid_mask)

    nrows, ncols = flow.nrows, flow.ncols
    out_columns = [c for c in source_gdf.columns if c != source_gdf.geometry.name]

    def empty_result() -> ConflationResult:
        return ConflationResult(gpd.GeoDataFrame(columns=out_columns + ["geometry"], geometry="geometry",
                                                 crs=source_gdf.crs), np.zeros((nrows, ncols), np.int32), {})

    if source_gdf.empty:
        return empty_result()

    source = prepare_source_network(source_gdf, id_col, ds_col, order_col, area_col, flow)
    n = source.ids.size
    if n == 0:
        return empty_result()
    
    if source.ids.max() > np.iinfo(np.int32).max:
        raise ValueError(f"Stream ids up to {source.ids.max()} do not fit the stream raster's 32-bit cells.")

    stop = np.zeros(nrows * ncols, np.bool_) if lake_mask is None else \
        np.ascontiguousarray(lake_mask, dtype=np.bool_).ravel()

    spacing_m = settings.sample_spacing_cells * flow.cell_size_m
    s_rows, s_cols, along, sample_offsets, cell_of_sample, inside = _sample_lines(source.geometry, flow, spacing_m)
    reach_of_sample = np.repeat(np.arange(n), np.diff(sample_offsets))
    usable = inside & flow.valid.ravel()[cell_of_sample] & ~stop[cell_of_sample]
    if dem is not None:
        dem = np.asarray(dem, np.float64).ravel()

    flow.build_channels(settings.min_channel_area_km2,
                        _inflow_cells(sample_offsets, usable, cell_of_sample, flow, dem, stop)[1])
    if flow.channel_cells.size == 0:
        return empty_result()

    # Search wider, and fit looser, along big rivers
    reach_area = np.where(source.area > 0, source.area, 0.0)
    reach_area = np.maximum(reach_area, _approximate_areas(source, flow))
    base_radius = max(settings.search_radius_m, settings.min_search_radius_cells * flow.cell_size_m)
    radius = np.clip(base_radius + settings.search_radius_per_sqrt_km2 * np.sqrt(reach_area), base_radius,
                     max(settings.max_search_radius_m, base_radius))
    sigma = settings.sigma_per_radius * radius
    sample_radius = radius[reach_of_sample]
    sample_sigma = sigma[reach_of_sample]

    cand, cand_dist, count = _find_candidates(s_rows, s_cols, usable, sample_radius, settings.max_candidates,
                                              flow.channel_id, stop, nrows, ncols, flow.dx_m, flow.dy_m,
                                              flow.channel_area_km2, np.zeros(s_rows.size),
                                              np.full(s_rows.size, np.inf))
    # How much of each reach could have been matched at all: inside the domain and outside lakes
    usable_length = np.bincount(reach_of_sample[usable], minlength=n).astype(np.float64)
    max_gap = max(1, round(settings.max_gap_m / spacing_m))
    beta_m, slack_m = settings.beta_cells * flow.cell_size_m, settings.slack_cells * flow.cell_size_m
    score, first_sample, last_sample, first_cid, last_cid, matched = _align_reaches(
        sample_offsets, usable & (count > 0), along, cand, cand_dist, count, flow.tin, flow.tout, flow.to_outlet_m,
        sample_sigma, beta_m, slack_m, max_gap, settings.gap_penalty)
    keep = (matched >= settings.min_matched_samples) & \
        (_matched_fraction(first_sample, last_sample, matched, usable_length) >= settings.min_matched_fraction)

    # Match the rest again, wider and looser (see fallback_radius_factor). A line the DEM's flow runs the other
    # way along -- the source network sends it upstream, or its ends were told apart wrongly -- can only match a
    # few cells as drawn, so each is also tried the other way round, and the better fit kept.
    loose = (usable_length > 0) & ~keep
    flipped = np.zeros(n, bool)   # matched against its line's direction
    if loose.any():
        sample_wide = (radius * settings.fallback_radius_factor)[reach_of_sample]

        def align_loosely(rows_f, cols_f, along_m, on):
            c, c_dist, c_count = _find_candidates(rows_f, cols_f, on, sample_wide, settings.max_candidates,
                                                  flow.channel_id, stop, nrows, ncols, flow.dx_m, flow.dy_m,
                                                  flow.channel_area_km2, np.zeros(rows_f.size),
                                                  np.full(rows_f.size, np.inf))
            return _align_reaches(sample_offsets, on & (c_count > 0), along_m, c, c_dist, c_count, flow.tin,
                                  flow.tout, flow.to_outlet_m, sample_wide / math.sqrt(2), beta_m, slack_m, max_gap,
                                  settings.gap_penalty)

        on_loose = usable & loose[reach_of_sample]
        forward = align_loosely(s_rows, s_cols, along, on_loose)
        # Each reach's samples in reverse order, and their distances along the reversed line
        last_of_reach = sample_offsets[reach_of_sample + 1] - 1
        flip = sample_offsets[reach_of_sample] + last_of_reach - np.arange(s_rows.size)
        backward = align_loosely(s_rows[flip], s_cols[flip], along[last_of_reach] - along[flip], on_loose[flip])
        backward_better = backward[0] > forward[0]
        for whole, ahead, behind in zip((score, first_sample, last_sample, first_cid, last_cid, matched), forward,
                                        backward):
            whole[loose] = np.where(backward_better, behind, ahead)[loose]
        flipped[loose] = backward_better[loose]

    placed = matched > 0
    matched_fraction = np.where(placed, _matched_fraction(first_sample, last_sample, matched, usable_length), 0.0)
    start = np.where(placed, flow.channel_cells[np.maximum(first_cid, 0)], -1)
    end = np.where(placed, flow.channel_cells[np.maximum(last_cid, 0)], -1)
    # How far each reach's own line runs on below its match, unmatched: how far _extend_ends may carry it to the reach
    # below (as well as max_extension_m)
    unmatched_m = _unmatched_below(sample_offsets, usable, last_sample, flipped) * spacing_m \
        * settings.unmatched_extension_factor

    # Each reach meets the flow of its main upstream reach (see _reconnect), repeated so a change can carry on down
    feeder = _main_feeders(placed, source, reach_area)
    reconnected = 0
    for _ in range(5):
        changed = _reconnect(start, end, feeder, flow.down, flow.channel_id, flow.tin, flow.tout, flow.to_outlet_m,
                             stop, source.ds, source.tin, source.tout, settings.max_extension_m,
                             settings.max_reconnect_m,
                             int(settings.max_reconnect_m / min(flow.dy_m, float(flow.dx_m.min()))) + 2)
        reconnected += changed
        if not changed:
            break

        feeder = _main_feeders(start >= 0, source, reach_area)

    placed = start >= 0

    snap_m = max(settings.snap_m, settings.min_snap_cells * flow.cell_size_m)
    claim_end = _extend_ends(start, end, flow.down, flow.channel_id, flow.to_outlet_m, stop, source.ds,
                             source.tin, source.tout, settings.max_extension_m, snap_m, unmatched_m)
    cells, label, first, last = _paint(start, claim_end, flow.down, flow.channel_id, flow.tin, flow.tout,
                                       flow.to_outlet_m, source.ds, source.tin, source.tout, source.order,
                                       reach_area, score, settings.max_extension_m)
    open_cell = flow.valid.ravel() & ~stop
    cells, label = _lengthen_short_reaches(cells, label, first, last, flow, open_cell, settings.min_reach_cells,
                                           source, reach_area, score)

    # Whatever still has no cells of its own gets a short flow path of free cells near its line
    has_cells = np.bincount(label[label >= 0], minlength=n) > 0
    stranded = np.flatnonzero((usable_length > 0) & ~has_cells)
    stubs = 0
    if stranded.size:
        owner = np.full(nrows * ncols, -1, np.int32)  # reach indices: a raster's worth, int32 halves it
        owner[cells[label >= 0]] = label[label >= 0]
        wide = radius * settings.fallback_radius_factor
        new_cells, new_labels = [cells], [label]
        for r in sorted(stranded, key=lambda r: (-source.order[r], -reach_area[r], -score[r], r)):
            a, b = sample_offsets[r], sample_offsets[r + 1]
            on = usable[a:b]
            stub = _stub_cells(s_rows[a:b][on], s_cols[a:b][on], float(wide[r]), owner, open_cell,
                               flow.channel_area_km2, flow.down, nrows, ncols, flow.dx_m, flow.dy_m,
                               max(int(on.sum()), 2))
            if stub.size:
                owner[stub] = r
                new_cells.append(stub)
                new_labels.append(np.full(stub.size, r, np.int64))
                stubs += 1

        cells, label = np.concatenate(new_cells), np.concatenate(new_labels)
        has_cells = np.bincount(label[label >= 0], minlength=n) > 0
        lost = source.ids[(usable_length > 0) & ~has_cells]
        if lost.size:
            LOG.warning(f"Conflation: {lost.size} reaches with part of their line on the DEM could not be placed: "
                        f"{lost.tolist()}.")

    drawn = _drawn_cells(cells, label, flow, source, reach_area, score, settings.min_reach_cells,
                         settings.min_beside_cells)
    outlets = 0
    if lake_mask is not None and dem is not None:
        drawn, outlets = _below_outlets(cells, label, drawn, flow, stop, dem, settings)

    raster = np.zeros(nrows * ncols, np.int32)
    raster[cells[drawn]] = source.ids[label[drawn]]
    raster = raster.reshape(nrows, ncols)

    streams = _reach_lines(cells, label, flow, source, source_gdf, id_col, ds_col, snap_m, drawn)
    LOG.info(
        f"Conflation: {len(streams)} of {n} source reaches placed on the D8 network ({int(keep.sum())} matched, "
        f"median matched fraction {np.median(matched_fraction[keep]) if keep.any() else 0:.2f}; "
        f"{int((placed & ~keep).sum())} matched with the wider radius, {reconnected} moved to meet the flow of "
        f"the reach above, {stubs} given a short flow path near their line, {outlets} started below the outlet "
        f"of the lake above)."
    )
    fraction = {int(source.ids[i]): float(matched_fraction[i]) for i in range(n)}
    return ConflationResult(streams, raster, fraction)


def _main_feeders(placed: np.ndarray, source: SourceNetwork, area: np.ndarray) -> np.ndarray:
    """
    For each reach, the placed reach draining into it that brings it at least half its flow (the largest by order,
    then area, if several do), or -1.
    """
    feeder = np.full(source.ids.size, -1, np.int64)
    for u in np.flatnonzero(placed & (source.ds >= 0)):
        r = source.ds[u]
        if area[u] < 0.5 * area[r]:
            continue

        x = feeder[r]
        if x < 0 or (source.order[u], area[u]) > (source.order[x], area[x]):
            feeder[r] = u

    return feeder


def _unmatched_below(sample_offsets: np.ndarray, usable: np.ndarray, last_sample: np.ndarray,
                     flipped: np.ndarray) -> np.ndarray:
    """
    How many usable samples of each reach's line lie past its match's last sample, in the direction it was matched
    (``flipped``: against its line, where ``last_sample`` indexes the reversed samples). 0 without a match.
    """
    n = sample_offsets.size - 1
    out = np.zeros(n)
    for r in range(n):
        if last_sample[r] < 0:
            continue

        a, b = int(sample_offsets[r]), int(sample_offsets[r + 1])
        if flipped[r]:
            # Position p of the reversed samples is sample a + b - 1 - p: those past it are the ones before that
            out[r] = usable[a:a + b - 1 - int(last_sample[r])].sum()
        else:
            out[r] = usable[int(last_sample[r]) + 1:b].sum()

    return out


def _matched_fraction(first_sample: np.ndarray, last_sample: np.ndarray, matched: np.ndarray,
                      usable_length: np.ndarray) -> np.ndarray:
    """The share of each reach's usable samples its match spans."""
    return np.where((matched > 0) & (usable_length > 0),
                    (last_sample - first_sample + 1) / np.maximum(usable_length, 1), 0.0)


def _sample_lines(lines: np.ndarray, flow: FlowNetwork, spacing_m: float):
    """
    Points every ``spacing_m`` along each line, sampled in pixel space scaled to metres so the spacing is even on
    a geographic grid. Returns their fractional rows and columns, distance along their line, the offsets of each
    line's points, the cell under each point, and whether it is inside the grid.
    """
    gt = flow.geotransform
    coords, index = shapely.get_coordinates(lines, return_index=True)
    cols_f = (coords[:, 0] - gt[0]) / gt[1]
    rows_f = (coords[:, 1] - gt[3]) / gt[5]
    dx_mid = float(np.median(flow.dx_m))
    offsets = np.searchsorted(index, np.arange(lines.size + 1)).astype(np.int64)
    sx, sy, along, sample_offsets = _resample(cols_f * dx_mid, rows_f * flow.dy_m, offsets, spacing_m)
    s_cols = sx / dx_mid
    s_rows = sy / flow.dy_m
    nrows, ncols = flow.nrows, flow.ncols
    inside = (s_rows >= 0) & (s_rows < nrows) & (s_cols >= 0) & (s_cols < ncols)
    cell_of_sample = np.where(inside, np.floor(s_rows).clip(0, nrows - 1).astype(np.int64) * ncols
                              + np.floor(s_cols).clip(0, ncols - 1).astype(np.int64), 0)
    return s_rows, s_cols, along, sample_offsets, cell_of_sample, inside


def _inflow_cells(sample_offsets: np.ndarray, usable: np.ndarray, cell_of_sample: np.ndarray, flow: FlowNetwork,
                  dem: np.ndarray | None = None, stop: np.ndarray | None = None,
                  max_candidates: int = 200) -> tuple[np.ndarray, np.ndarray]:
    """
    Where water the domain's DEM doesn't drain flows in along a source reach: on each reach that starts outside the
    domain, on nodata or in a lake, the first usable sample whose D8 path carries it into the domain (see
    _runs_inward). Near the domain's edge a D8 path often runs straight back out of it, which would lose the
    inflow. Where that sample is on the domain's edge, the water comes in at the river beside it (see
    _river_at_edge; ``dem`` is the filled DEM, flat, and no water comes in on ``stop``). Returns the reaches and
    their cells.
    """
    reaches, cells = [], []
    for r in range(sample_offsets.size - 1):
        a, b = sample_offsets[r], sample_offsets[r + 1]
        ok = np.flatnonzero(usable[a:b])
        if not ok.size or ok[0] == 0:
            continue

        candidates = cell_of_sample[a + ok]
        cell = next((c for c in candidates[:max_candidates] if _runs_inward(flow, int(c))), candidates[0])
        reaches.append(r)
        cells.append(_river_at_edge(flow, int(cell), dem, stop))

    return np.asarray(reaches, np.int64), np.asarray(cells, np.int64)


def _river_at_edge(flow: FlowNetwork, cell: int, dem: np.ndarray | None = None, stop: np.ndarray | None = None,
                   max_cells: int = 2) -> int:
    """
    Where a river whose source line crosses the domain's edge in ``cell`` comes in.

    A source line is often a cell or two off its river where it crosses the edge, on the bank. Its water coming in
    there would make the bank cell channel (it would drain it all), and the reach would start on it, metres above
    the river. So with ``dem`` (the filled DEM, flat) the river comes in at the lowest of ``cell`` and the cells on
    the edge within ``max_cells`` of it whose D8 paths run into the domain (see _runs_inward), the nearest if
    several are; without, at the last cell on the edge of ``cell``'s D8 path, at most ``max_cells`` down it: a bank
    drains to its river. Never on ``stop``, and ``cell`` as it is when it isn't on the edge.
    """
    if not _on_edge(flow, cell):
        return cell
    
    ncols, valid = flow.ncols, flow.valid.ravel()

    def open_edge(c: int) -> bool:
        return valid[c] and (stop is None or not stop[c]) and _on_edge(flow, c)

    if dem is None:
        for _ in range(max_cells):
            below = int(flow.down[cell])
            if below < 0 or not open_edge(below):
                break
            cell = below

        return cell
    
    row, col = divmod(cell, ncols)
    best, best_key = cell, (dem[cell], 0)
    for r in range(max(row - max_cells, 0), min(row + max_cells + 1, flow.nrows)):
        for c in range(max(col - max_cells, 0), min(col + max_cells + 1, ncols)):
            other = r * ncols + c
            key = (dem[other], (r - row) ** 2 + (c - col) ** 2)
            if other != cell and np.isfinite(key[0]) and key < best_key and open_edge(other) and _runs_inward(flow, other):
                best, best_key = other, key

    return best


def _on_edge(flow: FlowNetwork, cell: int) -> bool:
    """Whether ``cell`` is on the domain's edge: on the grid's, or next to nodata."""
    row, col = divmod(cell, flow.ncols)
    if min(row, col, flow.nrows - 1 - row, flow.ncols - 1 - col) == 0:
        return True
    
    return not flow.valid[row - 1:row + 2, col - 1:col + 2].all()


def _runs_inward(flow: FlowNetwork, cell: int, min_run: int = 10, depth: int = 20, max_steps: int = 500) -> bool:
    """
    Whether the D8 path from ``cell`` goes on for at least ``min_run`` cells and gets ``depth`` cells in from the
    grid's edge (or goes on for ``max_steps`` cells without ending).
    """
    for step in range(max_steps):
        row, col = divmod(cell, flow.ncols)
        if step >= min_run and min(row, col, flow.nrows - 1 - row, flow.ncols - 1 - col) >= depth:
            return True
        
        cell = flow.down[cell]
        if cell < 0:
            return False
        
    return True


def _approximate_areas(source: SourceNetwork, flow: FlowNetwork) -> np.ndarray:
    """Drainage area at each reach's downstream end, read off the DEM when the source network has none."""
    if (source.area > 0).all():
        return np.zeros(source.ids.size)
    
    ends = shapely.points([g.coords[-1] for g in source.geometry])
    return np.array([_area_near(flow, p) for p in ends], np.float64)


def _lengthen_short_reaches(cells: np.ndarray, label: np.ndarray, first: np.ndarray, last: np.ndarray,
                            flow: FlowNetwork, open_cell: np.ndarray, min_cells: int, source: SourceNetwork,
                            area: np.ndarray, score: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Lengthen every reach shorter than ``min_cells`` (at least two, the fewest a line can be drawn through) a cell at
    a time without changing where anything drains: by the free cell its last drains into, or else the last cell of
    a reach flowing into its first, or else the first cell of the reach its last drains into when nothing else
    flows into that one, as long as the reach giving up a cell keeps ``min_cells``. A reach that can't be lengthened
    is folded into the reach that flows into its first cell (the largest, if several do), or unlabelled when none
    does. Returns the cells, with any new ones appended, and their labels.
    """
    min_cells = max(min_cells, 2)
    counts = np.bincount(label[label >= 0], minlength=first.size)
    short = np.flatnonzero((counts > 0) & (counts < min_cells))
    if short.size == 0:
        return cells, label
    
    down, ncols, nrows = flow.down, flow.ncols, flow.nrows
    cells, label = list(cells.tolist()), list(label.tolist())
    index = {c: i for i, c in enumerate(cells)}

    def owner(cell: int) -> int:
        return label[index[cell]] if cell in index else -1

    def give(cell: int, r: int) -> None:
        if cell in index:
            label[index[cell]] = r
        else:
            index[cell] = len(cells)
            cells.append(cell)
            label.append(r)

    def rank(x: int) -> tuple:
        return source.order[x], area[x], score[x]

    # The reaches by their last cell, kept in step with ``last``: the reaches draining into a cell are then found among
    # the cells around it, rather than by going through every reach each time
    ends = {}
    for x in np.flatnonzero(last >= 0).tolist():
        ends.setdefault(int(last[x]), set()).add(x)

    def set_last(x: int, cell: int) -> None:
        if last[x] >= 0:
            ends[int(last[x])].discard(x)
        last[x] = cell
        if cell >= 0:
            ends.setdefault(int(cell), set()).add(x)

    def feeders(cell: int, r: int) -> list:
        """The reaches but r whose last cell drains into ``cell``, in order."""
        row, col = divmod(cell, ncols)
        found = []
        for d_row in (-1, 0, 1):
            for d_col in (-1, 0, 1):
                r_, c_ = row + d_row, col + d_col
                if (d_row or d_col) and 0 <= r_ < nrows and 0 <= c_ < ncols:
                    nb = r_ * ncols + c_
                    if down[nb] == cell:
                        found.extend(x for x in ends.get(nb, ()) if x != r)

        return sorted(found)

    def above(cell: int, x: int) -> int:
        """The cell of reach ``x`` that drains into ``cell``."""
        row, col = divmod(cell, ncols)
        for d_row in (-1, 0, 1):
            for d_col in (-1, 0, 1):
                r_, c_ = row + d_row, col + d_col
                if (d_row or d_col) and 0 <= r_ < nrows and 0 <= c_ < ncols:
                    nb = r_ * ncols + c_
                    if down[nb] == cell and owner(nb) == x:
                        return nb
                    
        return -1

    # Upstream first, so that a short reach below another short reach finds it already settled
    for r in sorted(short, key=lambda r: -flow.to_outlet_m[flow.channel_id[first[r]]]):
        while counts[r] < min_cells:
            below = int(down[last[r]])
            if below >= 0 and open_cell[below] and owner(below) < 0:
                give(below, r)
                set_last(r, below)
                counts[r] += 1
                continue

            donors = [x for x in feeders(int(first[r]), r) if counts[x] > min_cells]
            if donors:
                x = max(donors, key=rank)
                give(int(last[x]), r)
                first[r] = last[x]
                set_last(x, above(int(last[x]), x))
                counts[x] -= 1
                counts[r] += 1
                continue

            d = owner(below) if below >= 0 else -1
            if d >= 0 and first[d] == below and counts[d] > min_cells and not feeders(below, r):
                give(below, r)
                set_last(r, below)
                first[d] = down[below]
                counts[d] -= 1
                counts[r] += 1
                continue

            break

        if counts[r] >= min_cells:
            continue

        heirs = [x for x in feeders(int(first[r]), r) if counts[x] >= min_cells]
        heir = max(heirs, key=rank) if heirs else -1
        cell = int(first[r])
        while True:
            give(cell, heir)
            if cell == last[r]:
                break

            cell = int(down[cell])

        if heir >= 0:
            set_last(heir, int(last[r]))
            counts[heir] += counts[r]

        first[r] = -1
        set_last(r, -1)
        counts[r] = 0

    return np.asarray(cells, np.int64), np.asarray(label, np.int64)


@njit(cache=True)
def _stub_cells(rows_f: np.ndarray, cols_f: np.ndarray, radius_m: float, owner: np.ndarray, open_cell: np.ndarray,
                area: np.ndarray, down: np.ndarray, nrows: int, ncols: int, dx_m: np.ndarray, dy_m: float,
                max_cells: int) -> np.ndarray:
    """
    A short D8 path for a reach that has no cells of its own: from the free cell draining the most within
    ``radius_m`` of the reach's samples (fractional rows and columns) down over free cells until it joins the
    network, leaves the open cells or is ``max_cells`` long. A path that would be one cell long starts instead at
    the free cell draining the most into that cell. Empty when no free cell near the samples will do.
    """
    if rows_f.size == 0:
        return np.empty(0, np.int64)
    
    reach_r = math.ceil(radius_m / dy_m)
    reach_c = math.ceil(radius_m / dx_m.min())
    r0 = max(math.floor(rows_f.min()) - reach_r, 0)
    r1 = min(math.floor(rows_f.max()) + reach_r + 1, nrows)
    c0 = max(math.floor(cols_f.min()) - reach_c, 0)
    c1 = min(math.floor(cols_f.max()) + reach_c + 1, ncols)
    if r0 >= r1 or c0 >= c1:
        return np.empty(0, np.int64)
    
    near = np.zeros((r1 - r0, c1 - c0), np.bool_)
    for s in range(rows_f.size):
        r_mid = min(max(math.floor(rows_f[s]), 0), nrows - 1)
        sr = math.ceil(radius_m / dy_m)
        sc = math.ceil(radius_m / dx_m[r_mid])
        for r in range(max(r_mid - sr, r0), min(r_mid + sr + 1, r1)):
            for c in range(max(math.floor(cols_f[s]) - sc, c0), min(math.floor(cols_f[s]) + sc + 1, c1)):
                d = math.sqrt(((c + 0.5 - cols_f[s]) * dx_m[r]) ** 2 + ((r + 0.5 - rows_f[s]) * dy_m) ** 2)
                if d <= radius_m:
                    near[r - r0, c - c0] = True

    found = 0
    for r in range(r1 - r0):
        for c in range(c1 - c0):
            if near[r, c]:
                cell = (r + r0) * ncols + c + c0
                if owner[cell] < 0 and open_cell[cell]:
                    found += 1

    candidates = np.empty(found, np.int64)
    k = 0
    for r in range(r1 - r0):
        for c in range(c1 - c0):
            if near[r, c]:
                cell = (r + r0) * ncols + c + c0
                if owner[cell] < 0 and open_cell[cell]:
                    candidates[k] = cell
                    k += 1

    by_area = np.argsort(-area[candidates], kind="mergesort")
    path = np.empty(max(max_cells, 2), np.int64)
    for i in by_area:
        x = candidates[i]
        n = 1
        path[0] = x
        cell = x
        while n < max_cells:
            nxt = down[cell]
            if nxt < 0 or not open_cell[nxt] or owner[nxt] >= 0:
                break
            path[n] = nxt
            n += 1
            cell = nxt

        if n >= 2:
            return path[:n].copy()
        
        # One cell: start from the free cell draining the most into it
        row = x // ncols
        col = x % ncols
        best = -1
        for d_row in range(-1, 2):
            for d_col in range(-1, 2):
                rr = row + d_row
                cc = col + d_col
                if (d_row != 0 or d_col != 0) and 0 <= rr < nrows and 0 <= cc < ncols:
                    nb = rr * ncols + cc
                    if down[nb] == x and owner[nb] < 0 and open_cell[nb] and (best < 0 or area[nb] > area[best]):
                        best = nb

        if best >= 0:
            out = np.empty(2, np.int64)
            out[0] = best
            out[1] = x
            return out
        
    return np.empty(0, np.int64)


def _drawn_cells(cells: np.ndarray, label: np.ndarray, flow: FlowNetwork, source: SourceNetwork, area: np.ndarray,
                 score: np.ndarray, min_cells: int, min_beside: int) -> np.ndarray:
    """
    Which of the painted ``cells`` to draw: all of each reach's but the end of one that runs beside another reach
    before the two meet.

    D8 often takes a tributary down a path of its own beside the river it joins, a cell away, for a few cells
    before the two merge, and the tributary's cells there only repeat the river's. A reach whose last ``min_beside``
    cells or more are beside another reach ends at the first of them instead, a small gap above where it joins, and
    still drains into the same reach. Where two run beside each other, the smaller one (see _outranks) ends. A cell
    is beside another reach when one of its eight neighbours is that reach's and isn't downstream of it, so a river
    meandering back past itself isn't cut. Each reach keeps ``min_cells``.
    """
    labelled = label >= 0
    drawn = labelled.copy()
    if not labelled.any():
        return drawn
    
    nrows, ncols, down = flow.nrows, flow.ncols, flow.down
    owner = np.full(nrows * ncols, -1, np.int32)  # reach indices: a raster's worth, int32 halves it
    owner[cells[labelled]] = label[labelled]
    position = {int(c): i for i, c in enumerate(cells.tolist())}
    counts = np.bincount(label[labelled], minlength=source.ids.size)
    reach_cells, reach_label = cells[labelled], label[labelled]
    below = down[reach_cells]
    ends = (below < 0) | (owner[np.maximum(below, 0)] != reach_label)
    end_count = np.bincount(reach_label[ends], minlength=source.ids.size)
    # The last cell of each reach that is one unbroken path
    last = {int(r): int(c) for r, c in zip(reach_label[ends], reach_cells[ends]) if end_count[r] == 1}
    neighbours = [(d_row, d_col) for d_row in (-1, 0, 1) for d_col in (-1, 0, 1) if d_row or d_col]
    max_walk = nrows + ncols

    def downstream_of(cell: int, other: int) -> bool:
        """Whether ``other`` is on the D8 path down from ``cell``."""
        c, o = flow.channel_id[cell], flow.channel_id[other]
        if c >= 0:
            return bool(o >= 0 and flow.tin[o] <= flow.tin[c] <= flow.tout[o])
        
        for _ in range(max_walk):
            cell = down[cell]
            if cell < 0:
                return False
            if cell == other:
                return True
            
        return False

    def beside(cell: int, r: int) -> bool:
        """Whether ``cell`` of reach ``r`` is beside a reach that outranks r."""
        row, col = divmod(cell, ncols)
        for d_row, d_col in neighbours:
            rr, cc = row + d_row, col + d_col
            if 0 <= rr < nrows and 0 <= cc < ncols:
                nb = rr * ncols + cc
                x = owner[nb]
                if x >= 0 and x != r and _outranks(x, r, source.order, area, score) and not downstream_of(cell, nb):
                    return True
                
        return False

    def above(cell: int, r: int) -> int:
        """The cell of reach ``r`` that drains into ``cell``, or -1."""
        row, col = divmod(cell, ncols)
        for d_row, d_col in neighbours:
            rr, cc = row + d_row, col + d_col
            if 0 <= rr < nrows and 0 <= cc < ncols:
                nb = rr * ncols + cc
                if down[nb] == cell and owner[nb] == r:
                    return nb
                
        return -1

    # The larger reaches first, so that a reach beside one that ends sooner is only cut beside what is left of it
    for r in sorted(last, key=lambda r: (source.order[r], area[r], score[r], -r), reverse=True):
        run = []
        cell = last[r]
        while cell >= 0 and beside(cell, r):
            run.append(cell)
            cell = above(cell, r)

        if len(run) < min_beside:
            continue

        cut = min(len(run) - 1, counts[r] - min_cells)
        for cell in run[:max(cut, 0)]:
            owner[cell] = -1
            drawn[position[cell]] = False

    return drawn


def _below_outlets(cells: np.ndarray, label: np.ndarray, drawn: np.ndarray, flow: FlowNetwork, lake: np.ndarray,
                   dem: np.ndarray, settings: ConflationSettings) -> tuple[np.ndarray, int]:
    """
    Start each reach that flows out of a lake in the river below the lake's outlet, not on the dam, weir or shore
    cells between the two. Returns the ``drawn`` cells left, and how many reaches were moved.

    Lakes cut the network, so a reach leaving one starts at the first channel cell past its shore its line matched
    onto, which is often on the dam or the lake's rim, metres above the river at its foot. FLDPLN floods from every
    stream cell, and one perched like that floods the valley below at nearly the lake's level. A reach whose first
    cell is within ``outlet_lake_cells`` of a ``lake`` cell leaves the lake, and its outlet drops it from the lake
    in steps of at least ``outlet_drop_m`` from one cell to the next of ``dem`` (flat, the filled DEM). It starts
    below the last such step within ``max_outlet_m`` of its first cell, unless:

    - the river falls a step like that again within ``outlet_tailwater_cells`` below it: a steep stream rather than
      an outlet, which keeps its cells;
    - another reach flows into it: it starts at most where the first one joins it;
    - it would be left with fewer than ``min_reach_cells``.
    """
    labelled = drawn & (label >= 0)
    if not labelled.any():
        return drawn, 0
    
    nrows, ncols, down = flow.nrows, flow.ncols, flow.down
    lake_2d = lake.reshape(nrows, ncols)
    near = max(int(settings.outlet_lake_cells), 0)
    window = max(round(settings.max_outlet_m / flow.cell_size_m), settings.min_outlet_cells, 1)
    tail = max(int(settings.outlet_tailwater_cells), 0)
    min_cells = max(settings.min_reach_cells, 2)

    owner = np.full(nrows * ncols, -1, np.int32)  # reach indices: a raster's worth, int32 halves it
    owner[cells[labelled]] = label[labelled]
    position = {int(c): i for i, c in enumerate(cells.tolist())}
    reach_cells, reach_label = cells[labelled], label[labelled]
    counts = np.bincount(reach_label, minlength=int(label.max()) + 1)
    # Which cells a cell of the same reach drains into, and which a cell of another reach does
    below = down[reach_cells]
    flows_on = below >= 0
    same = np.zeros(nrows * ncols, bool)
    joined = np.zeros(nrows * ncols, bool)
    into_owner = owner[np.maximum(below, 0)]
    same[below[flows_on & (into_owner == reach_label)]] = True
    joined[below[flows_on & (into_owner >= 0) & (into_owner != reach_label)]] = True
    # Each reach's first cell: the one no cell of its own drains into (a reach that isn't one path is left alone)
    heads = ~same[reach_cells]
    head_count = np.bincount(reach_label[heads], minlength=counts.size)

    moved = 0
    for cell, r in zip(reach_cells[heads].tolist(), reach_label[heads].tolist()):
        if head_count[r] != 1 or joined[cell]:
            continue

        row, col = divmod(cell, ncols)
        if not lake_2d[max(row - near, 0):row + near + 1, max(col - near, 0):col + near + 1].any():
            continue

        path = [cell]
        while len(path) <= window + tail:
            nxt = int(down[path[-1]])
            if nxt < 0 or owner[nxt] != r:
                break
            path.append(nxt)

        z = dem[path]
        steps = z[:-1] - z[1:]
        # The outlet ends at the first reach joining this one, if that comes sooner
        limit = min(window, len(steps))
        for k in range(1, limit + 1):
            if joined[path[k]]:
                limit = k
                break

        drops = np.flatnonzero(steps[:limit] >= settings.outlet_drop_m)
        if not drops.size:
            continue

        foot = int(drops[-1]) + 1
        if (steps[foot:foot + tail] >= settings.outlet_drop_m).any() or counts[r] - foot < min_cells:
            continue

        for c in path[:foot]:
            owner[c] = -1
            drawn[position[c]] = False

        moved += 1

    return drawn, moved


def _reach_lines(cells: np.ndarray, label: np.ndarray, flow: FlowNetwork, source: SourceNetwork,
                 source_gdf: gpd.GeoDataFrame, id_col: str, ds_col: str, end_m: float,
                 drawn: np.ndarray | None = None) -> gpd.GeoDataFrame:
    """
    One row per painted reach: its ``drawn`` cells (default all) as a line, upstream to downstream, and the reach
    its painted cells drain into.

    A reach draining into another within ``end_m`` of that reach's last cell, where the source network doesn't
    send it there, drains where that reach does instead. Two reaches the source network sends into one that
    wasn't placed meet where it would have started: neither is the other's tributary, and a tributary at an
    outlet's very end reads as its head (ARC turns the outlet round to run away from it).
    """
    labelled = label >= 0
    if not labelled.any():
        columns = [c for c in source_gdf.columns if c != source_gdf.geometry.name]
        return gpd.GeoDataFrame(columns=columns + ["geometry"], geometry="geometry", crs=source_gdf.crs)
    
    reach_cells = cells[labelled]
    reach_label = label[labelled]
    # Order each reach's cells from upstream to downstream, by how far each is from the reach's last cell
    to_end = _to_reach_end(reach_cells, reach_label, flow.down, flow.ncols, flow.dx_m, flow.dy_m)
    order = np.lexsort((-to_end, reach_label))
    reach_cells = reach_cells[order]
    reach_label = reach_label[order]
    reach_drawn = (np.ones(labelled.sum(), bool) if drawn is None else drawn[labelled])[order]
    bounds = np.flatnonzero(np.diff(reach_label)) + 1
    starts = np.concatenate(([0], bounds))
    stops = np.concatenate((bounds, [reach_label.size]))

    cell_label = dict(zip(reach_cells.tolist(), reach_label.tolist()))
    cell_to_end = dict(zip(reach_cells.tolist(), to_end[order].tolist()))
    reaches = reach_label[starts]
    last_cells = reach_cells[stops - 1]
    row_of = {int(r): k for k, r in enumerate(reaches)}
    # The reach each one's last cell drains into, and whether that is within end_m of its last cell
    into = np.full(reaches.size, -1, np.int64)
    near_end = np.zeros(reaches.size, bool)
    for k, cell in enumerate(last_cells):
        downstream = int(flow.down[cell])
        x = cell_label.get(downstream, -1) if downstream >= 0 else -1
        if x < 0 or x == reaches[k]:
            continue

        into[k] = row_of[x]
        near_end[k] = cell_to_end[downstream] <= end_m

    xy = flow.cell_centres(reach_cells)
    rows = []
    for k, (a, b) in enumerate(zip(starts, stops)):
        r = int(reaches[k])
        hop, ds_row = k, into[k]
        while ds_row >= 0 and near_end[hop] and not _is_downstream(source.tin, source.tout, reaches[ds_row], r):
            hop, ds_row = ds_row, into[ds_row]

        ds_id = int(source.ids[reaches[ds_row]]) if ds_row >= 0 else -1
        rows.append((int(source.ids[r]), ds_id, shapely.linestrings(xy[a:b][reach_drawn[a:b]])))

    out = pd.DataFrame(rows, columns=[id_col, "__ds", "geometry"])

    attributes = source_gdf.drop(columns=[source_gdf.geometry.name]).copy()
    attributes[id_col] = pd.to_numeric(attributes[id_col], errors="coerce")
    attributes = attributes.drop_duplicates(subset=id_col).set_index(id_col)
    out = out.join(attributes, on=id_col)
    out[ds_col] = out.pop("__ds")
    columns = [id_col, ds_col] + [c for c in out.columns if c not in (id_col, ds_col, "geometry")] + ["geometry"]
    return gpd.GeoDataFrame(out[columns], geometry="geometry", crs=source_gdf.crs)


@njit(cache=True)
def _to_reach_end(cells: np.ndarray, label: np.ndarray, down: np.ndarray, ncols: int, dx_m: np.ndarray,
                  dy_m: float) -> np.ndarray:
    """Metres down the D8 path from each of ``cells`` to the last cell of its reach (``label``)."""
    m = cells.size
    order = np.argsort(cells)
    sorted_cells = cells[order]
    step = _step_lengths(cells, down, ncols, dx_m, dy_m)
    out = np.full(m, -1.0)
    walk = np.empty(m, np.int64)
    for i in range(m):
        if out[i] >= 0.0:
            continue

        k = 0
        j = i
        while True:
            walk[k] = j
            k += 1
            nxt = down[cells[j]]
            below = -1
            if nxt >= 0:
                p = np.searchsorted(sorted_cells, nxt)
                if p < m and sorted_cells[p] == nxt and label[order[p]] == label[j]:
                    below = order[p]

            if below < 0:
                out[j] = 0.0
                break

            if out[below] >= 0.0:
                out[j] = out[below] + step[j]
                break

            j = below

        for t in range(k - 2, -1, -1):
            out[walk[t]] = out[walk[t + 1]] + step[walk[t]]
            
    return out
