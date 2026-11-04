"""
Where a DEM shows its rivers' channels, whatever the DEM.

Some DEMs flatten their water (FABDEM and Copernicus to whole half metres, USGS 3DEP to any height), others store
heights in whole metres (SRTM, NASADEM, ASTER, AW3D30) or leave the water as noisy as the land. Two signs of a channel
hold across them, given the streams:

- Flat water: cells exactly as high as at least two of their neighbours, in patches of more than a few cells, no
  more than ``flat_rise_m`` above the river beside them, on a DEM whose heights are rounded to a centimetre or finer
  (FABDEM, Copernicus, USGS 3DEP). On a DEM rounded more coarsely (GEDTM's 0.1 m, SRTM's whole metres) most of the
  land is flat too, and flatness says nothing. Only within ``flat_widths`` times the river's expected width (see
  below) of the stream, and always within ``flat_min_m``, as far as ``snap_m`` allows a stream to be drawn off its
  water: FABDEM levels marshes and fields as well as water.
- Low ground by the stream: no more than ``rise_m`` above the river beside it, within ``widths`` of the river's
  expected width (``width_per_sqrt_km2`` metres per square root of the drainage area in km2) of the stream, along a
  river expected to be at least a cell wide. A narrower river's channel is its stream cells. Not on a DEM that
  flattens every river wide enough to show (FABDEM, Copernicus): it leaves unflattened only those too narrow for it,
  and their low ground is the floodplain beside them. A DEM that flattens only some of its water (USGS 3DEP), or none
  (one rounded coarsely, or that leaves its water as rough as the land), shows its other rivers by their low ground.
  Which DEM it is is ``flattens_rivers``, or when that is None, whether its flat water by the streams is mostly at
  whole half metres, as FABDEM and Copernicus flatten theirs (see flattens_its_rivers).

The river beside a cell is its nearest stream cell, whose level is the lowest of the DEM within ``snap_m`` of it (the
stream may be drawn a cell or two off the water), averaged along the streams over ``smooth_m``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numba import njit
from scipy.ndimage import label


@dataclass
class ChannelSettings:
    # How far from a stream a channel is looked for at all
    corridor_m: float = 1500.0
    # The river's level: the lowest of the DEM within snap_m of a stream cell, averaged along the streams over smooth_m
    snap_m: float = 60.0
    smooth_m: float = 150.0
    # Flat water: a cell as high as at least two neighbours, in a patch of more than flat_min_cells such cells, no more
    # than flat_rise_m above the river, on a DEM whose heights are rounded to no more than max_flat_step_m (see
    # height_step), within flat_widths times the river's expected width of the stream and always within flat_min_m
    # (flat_widths None: anywhere in the corridor)
    flat_min_cells: int = 6
    flat_rise_m: float = 2.0
    max_flat_step_m: float = 0.01
    flat_widths: float | None = 2.0
    flat_min_m: float = 60.0
    # Low ground: no more than rise_m above the river, within widths times the river's expected width of the stream,
    # along rivers expected to be at least min_width_cells wide (a narrower one's channel is its stream cells), on a
    # DEM that doesn't flatten every river wide enough to show
    rise_m: float = 1.0
    widths: float = 1.5
    width_per_sqrt_km2: float = 1.1
    min_width_cells: float = 1.0
    # Whether the DEM flattens every river wide enough to show (True: FABDEM, Copernicus) or not (False: USGS 3DEP);
    # None: it does when at least half_metre_share of its flat water by the streams is at whole half metres
    flattens_rivers: bool | None = None
    half_metre_share: float = 0.3


@dataclass
class Channel:
    """
    The flat water and the whole channel (the flat water, or on a DEM that doesn't flatten every river wide enough to
    show, the flat water and the low ground by the streams), as boolean rasters, or None for one river_channel was
    told not to work out.
    """
    flat_water: np.ndarray | None
    channel: np.ndarray | None


def flat_cells(dem: np.ndarray, valid: np.ndarray, min_cells: int = 6) -> np.ndarray:
    """Cells exactly as high as at least two of their neighbours, in 8-connected patches of more than ``min_cells``."""
    flat = _level_with_two_neighbours(np.asarray(dem), np.ascontiguousarray(valid, dtype=np.bool_))
    labels, _ = label(flat, np.ones((3, 3), int))
    # Only the few flat cells are counted and looked up, not the whole grid
    patch = labels[flat]
    out = np.zeros(flat.shape, bool)
    out[flat] = np.bincount(patch)[patch] > min_cells
    return out


@njit(cache=True)
def _level_with_two_neighbours(dem: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """The valid cells exactly as high as at least two of their valid neighbours."""
    nrows, ncols = dem.shape
    out = np.zeros((nrows, ncols), np.bool_)
    for r0 in range(nrows):
        for c0 in range(ncols):
            if not valid[r0, c0]:
                continue

            # The cell itself counts too, so three in its 3 x 3 block (none if it is NaN, which equals nothing)
            z = dem[r0, c0]
            equal = 0
            if 0 < r0 < nrows - 1 and 0 < c0 < ncols - 1:
                for r in (r0 - 1, r0, r0 + 1):
                    for c in (c0 - 1, c0, c0 + 1):
                        if dem[r, c] == z and valid[r, c]:
                            equal += 1
            else:
                for r in range(max(r0 - 1, 0), min(r0 + 2, nrows)):
                    for c in range(max(c0 - 1, 0), min(c0 + 2, ncols)):
                        if dem[r, c] == z and valid[r, c]:
                            equal += 1

            out[r0, c0] = equal >= 3

    return out


def height_step(dem: np.ndarray, valid: np.ndarray, sample: int = 200_000) -> float:
    """
    The step a DEM's heights are rounded to: the coarsest of 1 m, 0.1 m, 1 cm and 1 mm that at least 90% of its cells
    (up to ``sample`` of them) are whole multiples of, or 0 when they are continuous.
    """
    dem = np.asarray(dem)
    valid = np.ascontiguousarray(valid, dtype=np.bool_)
    n = int(np.count_nonzero(valid))
    if n > sample:
        # The sampled valid cells (their places among the valid cells, in row order), picked out without a copy of
        # all of them
        picked = np.sort(np.random.default_rng(0).choice(n, sample, replace=False))
        z = _nth_valid(np.ascontiguousarray(dem), valid, picked).astype(np.float32)
    else:
        z = dem[valid].astype(np.float32)

    if z.size == 0:
        return 0.0
    
    for step in (1.0, 0.1, 0.01, 0.001):
        if np.mean(np.float32(np.round(z.astype(np.float64) / step) * step) == z) >= 0.9:
            return step
        
    return 0.0


@njit(cache=True)
def _nth_valid(dem: np.ndarray, valid: np.ndarray, picked: np.ndarray) -> np.ndarray:
    """The heights of the ``picked``-th valid cells (ascending places among the valid cells, in row order)."""
    out = np.empty(picked.size, dem.dtype)
    flat_dem, flat_valid = dem.ravel(), valid.ravel()
    seen = 0
    j = 0
    for i in range(flat_valid.size):
        if j == picked.size:
            break

        if flat_valid[i]:
            while j < picked.size and picked[j] == seen:
                out[j] = flat_dem[i]
                j += 1
            seen += 1

    return out


def touching(mask: np.ndarray, streams: np.ndarray) -> np.ndarray:
    """The 8-connected patches of ``mask`` that touch a stream cell, or a cell next to one."""
    out = np.zeros(np.shape(mask), np.bool_)
    flood_patches(np.ascontiguousarray(mask, dtype=np.bool_), np.flatnonzero(np.asarray(streams) > 0), 1, out)
    return out


@njit(cache=True)
def flood_patches(mask: np.ndarray, cells: np.ndarray, reach: int, out: np.ndarray) -> None:
    """
    Set ``out`` True on the 8-connected patches of ``mask`` with a cell within ``reach`` cells (a square) of one of
    ``cells`` (flat indices): the patches scipy's label(mask, np.ones((3, 3))) finds there, flooded from those cells
    rather than labelled across the grid. Patches already in ``out`` are left as they are.
    """
    nrows, ncols = mask.shape
    queue = np.empty(1024, np.int64)
    for i in range(cells.size):
        r0, c0 = cells[i] // ncols, cells[i] % ncols
        for r in range(max(r0 - reach, 0), min(r0 + reach + 1, nrows)):
            for c in range(max(c0 - reach, 0), min(c0 + reach + 1, ncols)):
                if not mask[r, c] or out[r, c]:
                    continue
                out[r, c] = True
                queue[0] = r * ncols + c
                head, tail = 0, 1
                while head < tail:
                    cell = queue[head]
                    head += 1
                    row, col = cell // ncols, cell % ncols
                    for nr in range(max(row - 1, 0), min(row + 2, nrows)):
                        for nc in range(max(col - 1, 0), min(col + 2, ncols)):
                            if mask[nr, nc] and not out[nr, nc]:
                                out[nr, nc] = True
                                if tail == queue.size:
                                    grown = np.empty(queue.size * 2, np.int64)
                                    grown[:tail] = queue[:tail]
                                    queue = grown
                                queue[tail] = nr * ncols + nc
                                tail += 1


def river_channel(dem: np.ndarray, valid: np.ndarray, streams: np.ndarray, area_km2: dict, dx_m: float, dy_m: float,
                  settings: ChannelSettings | None = None, connected: bool = True, flat_water: bool = True,
                  channel: bool = True, double: bool = False) -> Channel:
    """
    The channel the DEM shows along ``streams`` (each stream cell's reach id, 0 elsewhere): see the module. ``area_km2``
    is each reach's drainage area, by id (a reach without one is taken as draining 1 km2); ``dx_m`` and ``dy_m`` are
    the cell's size. With ``connected``, only the patches touching a stream are kept. With ``flat_water`` or
    ``channel`` False, that one isn't worked out, and is None. With ``double``, the river's level is averaged in double
    precision on a single-precision DEM too, as on a float64 copy of it (see _stream_levels).
    """
    s = settings or ChannelSettings()
    valid = np.ascontiguousarray(valid, dtype=np.bool_)
    streams = np.asarray(streams)
    on_stream = valid & (streams > 0)
    empty = np.zeros(dem.shape, bool)
    if not on_stream.any():
        return Channel(empty if flat_water else None, empty.copy() if channel else None)
    
    cell = math.sqrt(dx_m * dy_m)
    flat = flat_cells(dem, valid, s.flat_min_cells) if height_step(dem, valid) <= s.max_flat_step_m else None
    if not channel and flat is None:
        return Channel(empty, None)

    # The river's level at each stream cell, and the river beside each cell: its nearest stream cell. The stream cells
    # are in row order, so each row's start among them finds one's number without a grid of the numbers.
    rows, cols, level = _stream_levels(dem, valid, on_stream, cell, s, double)
    have = np.zeros(dem.shape, bool)
    have[rows, cols] = True
    sampling = (dy_m, dx_m)
    nearest = nearest_cell(have, sampling)
    del have

    row_start = np.searchsorted(rows, np.arange(dem.shape[0] + 1))

    # The expected width of each stream cell's river, and how far from it flat water reaches
    ids, of_reach = np.unique(streams[rows, cols], return_inverse=True)
    width = (s.width_per_sqrt_km2 * np.sqrt([max(area_km2.get(int(i), 1.0), 0.01) for i in ids]))[of_reach]
    if s.flat_widths is None:
        flat_reach = np.full(rows.size, s.corridor_m)
    else:
        flat_reach = np.minimum(np.maximum(s.flat_widths * width, s.flat_min_m), s.corridor_m)

    water = np.zeros(dem.shape, bool)
    if not channel:
        # Only the flat cells' rise above the river is needed
        _beside_the_river(dem, valid, flat, nearest, row_start, cols, level, width, flat_reach, sampling[0],
                          sampling[1], s.corridor_m, s.flat_rise_m, s.rise_m, s.widths, False, water, water)
        del nearest

        return Channel(touching(water, on_stream) if connected else water, None)

    # Low ground only along rivers expected to be at least min_width_cells wide
    width = np.where(width >= s.min_width_cells * cell, width, 0.0)
    low = np.zeros(dem.shape, bool)
    _beside_the_river(dem, valid, np.zeros((0, 0), np.bool_) if flat is None else flat, nearest, row_start, cols,
                      level, width, flat_reach, sampling[0], sampling[1], s.corridor_m, s.flat_rise_m, s.rise_m,
                      s.widths, True, water, low)
    del nearest, flat

    if connected:
        water = touching(water, on_stream)

    # A DEM that flattens its rivers' water shows their channels by it, and the low ground by a river it left
    # unflattened is the floodplain of one too narrow for it (see the module)
    if water.any() and flattens_its_rivers(dem, water, s):
        whole = water.copy()
    else:
        whole = np.logical_or(low, water, out=low)
        if connected:
            whole = touching(whole, on_stream)

    return Channel(water if flat_water else None, whole)


def flattens_its_rivers(dem: np.ndarray, water: np.ndarray, settings: ChannelSettings | None = None) -> bool:
    """
    Whether the DEM flattens every river wide enough to show: ``settings.flattens_rivers``, or when that is None,
    whether at least ``half_metre_share`` of its flat ``water`` (a boolean raster) is at whole half metres, as FABDEM
    and Copernicus flatten their water. USGS 3DEP flattens some of its water, to any height. The share of the rivers 
    expected wide that have flat water beside them sets the DEMs less far apart.
    """
    s = settings or ChannelSettings()
    if s.flattens_rivers is not None:
        return bool(s.flattens_rivers)

    heights = np.asarray(dem)[water].astype(np.float64)
    if heights.size == 0:
        return False

    return bool(np.mean(heights * 2 == np.round(heights * 2)) >= s.half_metre_share)


@njit(cache=True)
def _beside_the_river(dem: np.ndarray, valid: np.ndarray, flat: np.ndarray, nearest: np.ndarray,
                      row_start: np.ndarray, cols: np.ndarray, level: np.ndarray, width: np.ndarray,
                      flat_reach: np.ndarray, s0: float, s1: float, corridor_m: float, flat_rise_m: float,
                      rise_m: float, widths: float, channel: bool, water: np.ndarray, low: np.ndarray) -> None:
    """
    river_channel's flat water and, with ``channel``, its low ground, set in ``water`` and ``low``, a cell at a time.
    The river beside a cell is its ``nearest`` stream cell (a flat index), the k-th with a ``level``, and its
    ``width`` and ``flat_reach``: the stream cells are in row order, row r's from the row_start[r]-th, at ``cols``.
    Distances are in metres, rows ``s0`` and columns ``s1`` apart, as _feature_distance has them. Without
    ``channel`` only the ``flat`` cells are looked at; with it, every cell, and ``flat`` may be empty: no flat water.
    """
    nrows, ncols = dem.shape
    with_flat = flat.size > 0
    # Cells next to each other mostly share their nearest stream cell, whose number is looked up once
    last, near_row, near_col, k = -1, 0, 0, 0
    for r in range(nrows):
        for c in range(ncols):
            is_flat = with_flat and flat[r, c]
            if not channel and not is_flat:
                continue

            near = nearest[r, c]
            if near != last:
                last = near
                near_row = near // ncols
                near_col = near - near_row * ncols
                first = row_start[near_row]
                k = first + np.searchsorted(cols[first:row_start[near_row + 1]], near_col)
                
            d_row = float(near_row - r) * s0
            d_col = float(near_col - c) * s1
            distance = np.sqrt(d_row * d_row + d_col * d_col)
            rise = np.float64(dem[r, c]) - level[k]
            if not channel:
                water[r, c] = distance <= flat_reach[k] and rise <= flat_rise_m
                continue

            corridor = valid[r, c] and distance <= corridor_m
            if is_flat:
                water[r, c] = corridor and rise <= flat_rise_m and distance <= flat_reach[k]

            w = width[k]
            low[r, c] = corridor and rise <= rise_m and w > 0 and distance <= widths * w


def within_distance(cells: np.ndarray, sampling: tuple[float, float], distance: float) -> np.ndarray:
    """
    The cells no farther than ``distance`` from the nearest of ``cells`` (a boolean raster, with at least one True), the
    cells ``sampling`` (height, width) apart: distance_transform_edt(~cells, sampling) <= distance, to the last bit.
    """
    return _within(nearest_cell(cells, sampling), float(sampling[0]), float(sampling[1]), float(distance))


@njit(cache=True)
def _within(nearest: np.ndarray, s0: float, s1: float, distance: float) -> np.ndarray:
    """
    Whether each cell is no farther than ``distance`` from its ``nearest`` cell (a flat index), as _feature_distance
    has it.
    """
    nrows, ncols = nearest.shape
    out = np.empty((nrows, ncols), np.bool_)
    for r in range(nrows):
        for c in range(ncols):
            near_row = nearest[r, c] // ncols
            d_row = float(near_row - r) * s0
            d_col = float(nearest[r, c] - near_row * ncols - c) * s1
            out[r, c] = np.sqrt(d_row * d_row + d_col * d_col) <= distance

    return out


def nearest_cell(cells: np.ndarray, sampling: tuple[float, float] = (1.0, 1.0)) -> np.ndarray:
    """
    The flat index of the nearest of ``cells`` (a boolean raster, with at least one True) to each cell, the cells
    ``sampling`` (height, width) apart: the cell scipy's distance_transform_edt(~cells, sampling,
    return_indices=True) gives, ties and all, in half its memory.
    """
    cells = np.ascontiguousarray(cells, dtype=np.bool_)
    out = np.empty(cells.shape, np.int32 if cells.size < np.iinfo(np.int32).max else np.int64)
    _feature_transform(cells, float(sampling[0]), float(sampling[1]), out)
    return out


@njit(cache=True)
def _feature_transform(feature: np.ndarray, s0: float, s1: float, out: np.ndarray) -> None:
    """
    Set ``out`` to the flat index of the nearest ``feature`` cell to each cell, rows ``s0`` and columns ``s1`` apart,
    by scipy's exact Euclidean feature transform (Maurer et al. 2003; ni_morphology.c), in its arithmetic: down each
    column to its nearest feature, the upper one of two as near, and then along each row to the nearest of those, by
    the lower envelope of their parabolas. Left -1 without features.
    """
    nrows, ncols = feature.shape
    above = np.full(ncols, -1, np.int64)
    for r in range(nrows):
        for c in range(ncols):
            if feature[r, c]:
                above[c] = r
            out[r, c] = above[c]

    below = np.full(ncols, -1, np.int64)
    for r in range(nrows - 1, -1, -1):
        for c in range(ncols):
            if feature[r, c]:
                below[c] = r

            up = out[r, c]
            if below[c] >= 0 and (up < 0 or r - up > below[c] - r):
                out[r, c] = below[c]

    # Each row's columns, by the row of their nearest feature (-1 for none), and the columns on the envelope
    frow = np.empty(ncols, np.int64)
    g = np.empty(ncols, np.int64)
    for r in range(nrows):
        for c in range(ncols):
            frow[c] = out[r, c]

        last = -1
        for c in range(ncols):
            if frow[c] < 0:
                continue

            tw = float(frow[c] - r) * s0
            w = 0.0 + tw * tw
            while last >= 1:
                c1, c2 = g[last], g[last - 1]
                a = (float(c1) - float(c2)) * s1
                b = (float(c) - float(c1)) * s1
                ab = a + b
                tu = (float(frow[c2]) - float(r)) * s0
                tv = (float(frow[c1]) - float(r)) * s0
                u = 0.0 + tu * tu
                v = 0.0 + tv * tv
                if ab * v - b * u - a * w - a * b * ab <= 0.0:
                    break
                last -= 1

            last += 1
            g[last] = c
            
        if last < 0:
            for c in range(ncols):
                out[r, c] = -1
            continue

        k = 0
        for c in range(ncols):
            t0 = float(frow[g[k]] - r) * s0
            t1 = float(g[k] - c) * s1
            here = 0.0 + t0 * t0 + t1 * t1
            while k < last:
                t0 = float(frow[g[k + 1]] - r) * s0
                t1 = float(g[k + 1] - c) * s1
                after = 0.0 + t0 * t0 + t1 * t1
                if here <= after:
                    break
                here = after
                k += 1

            out[r, c] = frow[g[k]] * ncols + g[k]


def _feature_distance(d_row: np.ndarray, d_col: np.ndarray, sampling: tuple[float, float]) -> np.ndarray:
    """
    The distance in metres to a cell ``d_row`` rows and ``d_col`` columns away (``sampling``: the cells' height and
    width), worked out as scipy's distance_transform_edt works it out from its feature transform, to the last bit.
    """
    d_row = d_row.astype(np.float64)
    d_col = d_col.astype(np.float64)
    d_row *= sampling[0]
    d_col *= sampling[1]
    d_row *= d_row
    d_col *= d_col
    d_row += d_col
    return np.sqrt(d_row, out=d_row)


def _stream_levels(dem: np.ndarray, valid: np.ndarray, on_stream: np.ndarray, cell: float, s: ChannelSettings,
                   double: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    The river's level at the stream cells: the lowest of the DEM within snap_m of each, averaged along the streams by a
    Gaussian of smooth_m. Returns the stream cells' rows and columns, and their levels.

    Worked out at the stream cells alone, but exactly as a minimum filter and scipy's gaussian_filter (reflected at the
    edges, truncated at 4 sigma) over the whole grid work it out, to the last bit: in the DEM's precision when that is
    single (see _smoothed_at), unless ``double``.
    """
    k = max(round(s.snap_m / cell), 1)
    rows, cols = np.nonzero(on_stream)
    lowest = _window_min(dem, valid, rows, cols, k)
    finite = np.isfinite(lowest)
    rows, cols, lowest = rows[finite], cols[finite], lowest[finite]

    # gaussian_filter's kernel: truncated at 4 sigma, normalised to sum to 1
    sigma = float(max(s.smooth_m / cell, 1.0))
    radius = int(4.0 * sigma + 0.5)
    x = np.arange(-radius, radius + 1)
    kernel = np.exp(-0.5 / (sigma * sigma) * x ** 2)
    kernel = np.ascontiguousarray((kernel / kernel.sum())[::-1])
    # A minimum filter over the DEM works in its type, or in float64 if that holds no infinity. The lowest heights are
    # the DEM's own, so a single-precision DEM's are held in single precision whichever the filters work in.
    work = np.where(np.ones(1, bool), np.zeros(1, dem.dtype), np.inf).dtype
    values = np.zeros(dem.shape, np.float32 if dem.dtype == np.float32 else np.float64)
    values[rows, cols] = lowest
    weight = np.zeros(dem.shape, np.bool_)
    weight[rows, cols] = True
    return rows, cols, _smoothed_at(values, weight, rows, cols, kernel, work == np.float32 and not double)


@njit(cache=True)
def _window_min(dem: np.ndarray, valid: np.ndarray, rows: np.ndarray, cols: np.ndarray, k: int) -> np.ndarray:
    """The lowest valid cell of ``dem`` within ``k`` cells (a square) of each of the cells, inf where there is none."""
    nrows, ncols = dem.shape
    out = np.full(rows.size, np.inf)
    for i in range(rows.size):
        for r in range(max(rows[i] - k, 0), min(rows[i] + k + 1, nrows)):
            for c in range(max(cols[i] - k, 0), min(cols[i] + k + 1, ncols)):
                if valid[r, c] and dem[r, c] < out[i]:
                    out[i] = dem[r, c]
    return out


@njit(cache=True)
def _reflect(i: int, n: int) -> int:
    """Index ``i`` of a line of ``n`` reflected back into it about its ends (d c b a | a b c d | d c b a)."""
    m = i % (2 * n)
    return m if m < n else 2 * n - 1 - m


@njit(cache=True)
def _smoothed_at(values: np.ndarray, weight: np.ndarray, rows: np.ndarray, cols: np.ndarray, kernel: np.ndarray,
                 single: bool) -> np.ndarray:
    """
    At each of the cells, gaussian_filter(values) / max(gaussian_filter(weight), 1e-12), with scipy's symmetric
    ``kernel`` and its reflected edges, summed in the order scipy sums them (down the columns, then along the rows,
    each pair of cells either side of the middle from the outermost in) so the result is the same to the last bit.
    With ``single``, ``values`` are float32 and each pass is rounded to float32, as scipy stores them.

    ``values`` are 0 off ``weight``, which is sparse: down a column, a pair of cells neither of which ``weight`` has
    adds nothing, and is skipped. Each column's ``weight`` cells are listed by row, and the cells worked out from the
    top down, so where each column's cells near a cell start is found by moving on from where they started for the one
    before. Only near the grid's edges, where the kernel reflects, is every pair added. A column's sum down it at a
    row is the same whichever cell of the row it is for, so the cells of a row share theirs.
    """
    nrows, ncols = values.shape
    half = kernel.size // 2
    middle = kernel[half]
    down_v = np.empty(kernel.size)
    down_w = np.empty(kernel.size)
    out = np.empty(rows.size)
    # The sums down each column at the row they were last worked out for
    sum_v = np.empty(ncols)
    sum_w = np.empty(ncols)
    sum_row = np.full(ncols, -1, np.int64)
    # The weight cells of each column, by row: col_rows[col_start[col]:col_start[col + 1]]
    col_start = np.zeros(ncols + 1, np.int64)
    for r in range(nrows):
        for c in range(ncols):
            if weight[r, c]:
                col_start[c + 1] += 1

    for c in range(ncols):
        col_start[c + 1] += col_start[c]

    col_rows = np.empty(col_start[ncols], np.int64)
    filled = col_start[:ncols].copy()
    for r in range(nrows):
        for c in range(ncols):
            if weight[r, c]:
                col_rows[filled[c]] = r
                filled[c] += 1

    # Each column's first weight cell not above the window of the cell at hand, and its first below it
    first = col_start[:ncols].copy()
    after = col_start[:ncols].copy()
    for i in np.argsort(rows, kind="mergesort"):
        r, c = rows[i], cols[i]
        inside_rows = half <= r < nrows - half
        inside = inside_rows and half <= c < ncols - half
        for j in range(-half, half + 1):
            col = c + j if 0 <= c + j < ncols else _reflect(c + j, ncols)
            if sum_row[col] == r:
                down_v[j + half] = sum_v[col]
                down_w[j + half] = sum_w[col]
                continue

            v = np.float64(values[r, col]) * middle
            w = (1.0 if weight[r, col] else 0.0) * middle
            if inside:
                while first[col] < col_start[col + 1] and col_rows[first[col]] < r - half:
                    first[col] += 1

                while after[col] < col_start[col + 1] and col_rows[after[col]] <= r + half:
                    after[col] += 1

                lo, hi = first[col], after[col] - 1
                # The pairs either side of the middle with a weight cell, from the outermost in
                pairs = 0
                while True:
                    d_above = r - col_rows[lo] if lo <= hi and col_rows[lo] < r else 0
                    d_below = col_rows[hi] - r if hi >= lo and col_rows[hi] > r else 0
                    d = max(d_above, d_below)
                    if d == 0:
                        break
                    v += (np.float64(values[r - d, col]) + np.float64(values[r + d, col])) * kernel[half - d]
                    w += ((1.0 if weight[r - d, col] else 0.0)
                          + (1.0 if weight[r + d, col] else 0.0)) * kernel[half - d]
                    pairs += 1
                    if d_above == d:
                        lo += 1
                    if d_below == d:
                        hi -= 1

                if pairs < half:
                    v += 0.0  # as a skipped pair would have: -0.0 becomes 0.0
            else:
                for jj in range(-half, 0):
                    if inside_rows:
                        above, below = r + jj, r - jj
                    else:
                        above, below = _reflect(r + jj, nrows), _reflect(r - jj, nrows)
                    v += (np.float64(values[above, col]) + np.float64(values[below, col])) * kernel[half + jj]
                    w += ((1.0 if weight[above, col] else 0.0)
                          + (1.0 if weight[below, col] else 0.0)) * kernel[half + jj]
                    
            down_v[j + half] = np.float64(np.float32(v)) if single else v
            down_w[j + half] = w
            sum_v[col], sum_w[col], sum_row[col] = down_v[j + half], w, r

        total = down_v[half] * middle
        norm = down_w[half] * middle
        for jj in range(-half, 0):
            total += (down_v[half + jj] + down_v[half - jj]) * kernel[half + jj]
            norm += (down_w[half + jj] + down_w[half - jj]) * kernel[half + jj]

        if single:
            total = np.float64(np.float32(total))

        out[i] = total / max(norm, 1e-12)
        
    return out
