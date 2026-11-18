"""
Move source reaches that the DEM shows are off their river onto it, before they are burned into it.

Burning a source network into a DEM cuts each river where the source draws it. Where the source is wrong --
GEOGLOWS draws a river straight across its floodplain, or along the rim of its canyon -- the burn cuts a new
channel there and the river is mapped in the wrong place.

A line drawn on its river follows the DEM's flow paths, and a line drawn beside it -- up the bank, across a bar or a
terrace -- drains onto it: the D8 paths from the cells under a line converge on the channel it stands for. So for
each reach, on the unburned DEM filled, a D8 path is traced from every cell under its line, and each cell counts the
paths through it (its votes). The reach's path runs up from the most-voted cell nearest the line's end through the
most-voted cells flowing into it (see _Relocation._path). But D8 paths cross the flats filling leaves in straight
lines, and cut meanders the DEM is too coarse to show, so a reach only moves onto its path where the DEM shows a
channel along the path that its line misses, and the path is about as long as the line or the line runs well above it
(see RelocationSettings), and drains on into the next reach. Moved reaches are carried down their D8 path onto the next
reach's line, but not on past its start (see RelocationSettings.junction_m); where neither a reach nor the reach it
flows into gets to the other that way, the moved ones keep their lines where their paths don't reach (see
_Relocation._keeping_lines), so the network stays joined.

A river flowing in across the domain's edge drains little within it, so what the source says it drains is added to
the flow paths from where it comes in: at the first cell whose flow path follows it, and again wherever it comes back
in after leaving the domain (see _inflow_entries). Where the DEM dams a river -- a gorge narrower than a cell --
filling floods the valley above, which can then drain some other way; breach_dammed_rivers burns the source line
through such places before the DEM is filled.
"""
from __future__ import annotations

import heapq
import warnings
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from numba import njit
from scipy.spatial import cKDTree

from nencarta.logger import LOG
from nencarta.tasks.channel_detection import (
    ChannelSettings,
    river_channel,
    within_distance,
)
from nencarta.tasks.stream_conflation import (
    FlowNetwork,
    SourceNetwork,
    _inflow_cells,
    _sample_lines,
    prepare_source_network,
)


@dataclass
class RelocationSettings:
    """
    When a reach is moved onto the D8 network of the unburned DEM. Distances are in metres, so they mean the same on
    any DEM, with floors in cells where the DEM's resolution is the limit.
    """
    # The D8 paths voted for are traced within this far of the line: grows with the square root of the reach's
    # drainage area (km2), and stop at a river draining more than area_factor times the reach. Below a reach others
    # flow into, the path starts where it drains what flows into the reach over area_factor -- what the source says
    # flows in, or what the DEM drains within that far of the line's start, if less.
    search_radius_m: float = 500.0
    search_radius_per_sqrt_km2: float = 10.0
    max_search_radius_m: float = 2500.0
    area_factor: float = 2.0
    # The path ends among the cells with at least this fraction of the most votes, at the one nearest the line's end
    plateau: float = 0.8
    # Only where the DEM shows a channel: a reach moves only where at least min_channel_share of its path's cells are
    # channel cells, and min_channel_gain more of them than of its line's. A channel cell is on the water the DEM
    # flattens near the lines' level (see channel_detection), or, off ground the
    # fill raised, at least channel_depth_m below both the cells channel_reach_cells away along a row, column or
    # diagonal. Across flats the filled DEM's D8 paths are arbitrary.
    min_channel_share: float = 0.5
    min_channel_gain: float = 0.2
    # A line on its river but for a short cut across the neck of a bend dilutes that comparison, so a reach also moves
    # where its path, following the bend, is at least bend_length_ratio times as long as the stretch of line it stands
    # for, and over just the cells where its line and path are more than 1.5 cells apart (at least 5 of each), the
    # path's share of channel cells is at least bend_channel_gain more than the line's. None: never.
    bend_channel_gain: float | None = 0.4
    bend_length_ratio: float = 1.1
    channel_depth_m: float = 0.3
    channel_reach_cells: int = 2
    # Protect meanders: a reach keeps its line where its path is shorter than this share of the stretch of line it
    # stands for (between where the path's ends are nearest the line): D8 cuts meanders across what the fill leveled,
    # while a D8 path following the same course is up to 8% longer than it. Unless the line runs a median of
    # meander_max_rise_m or more above its path (each of its vertices against the nearest path cell, on the DEM before
    # filling): a line up a bank or a valley side and back is longer than the river without meandering, while
    # meanders run on the floodplain, as low as a path cutting across them.
    min_length_ratio: float = 0.9
    meander_max_rise_m: float = 1.5
    # A moved reach is carried down D8 onto the line of the reach it flows into. Where that line is drawn a cell or
    # more off the river, D8 carries it on past the line's start, down the river beside it to where they cross, or a
    # reach or more down. So where the reach it flows into kept its line, a path that comes within junction_m of its
    # start (at least 1.5 cells) but doesn't meet its line that near ends at its cell nearest the start, and keeps its
    # line below there
    junction_m: float = 150.0
    # Where the DEM under a line rises dam_height_m or more and comes back down within max_dam_reaches reaches,
    # with no way round less high, the DEM dams the river, and the line is burned through (see breach_dammed_rivers):
    # from a low with the cell above it within dam_end_step_m of it, to one with the cell below it so
    dam_height_m: float = 40.0
    max_dam_reaches: int = 5
    dam_end_step_m: float = 1.0
    # A river flowing in across the domain's edge takes in its water where its D8 path follows its course at least
    # min_inflow_run_m before leaving the domain, looked for down at most max_course_m of it (see _inflow_entries)
    min_inflow_run_m: float = 1000.0
    max_course_m: float = 50000.0


def relocate_misplaced_reaches(
        source_gdf: gpd.GeoDataFrame,
        flowdir: np.ndarray,
        filled: np.ndarray,
        dem: np.ndarray,
        geotransform: tuple,
        projection: str,
        id_col: str,
        ds_col: str,
        area_col: str,
        valid_mask: np.ndarray | None = None,
        settings: RelocationSettings | None = None,
) -> gpd.GeoDataFrame:
    """
    ``source_gdf`` with the reaches the DEM shows are off their river moved onto it (see the module), on the DEM
    ``dem`` (before filling; in ``source_gdf``'s CRS), whose D8 pointers are ``flowdir`` (see
    stream_conflation.D8_OFFSETS) and whose filled heights are ``filled``. ``area_col`` is the drainage area (km2) at
    each reach's outlet. Rows keep every column and their drawing direction; a moved reach in several parts becomes the
    path of its main stem, and a reach running off the DEM keeps its line there.
    """
    settings = settings or RelocationSettings()
    if source_gdf.empty:
        return source_gdf
    
    relocation = _Relocation(source_gdf, flowdir, filled, dem, geotransform, projection, id_col, ds_col, area_col,
                             valid_mask, settings)
    relocation.run()
    LOG.info(f"{len(relocation.paths)} of {len(source_gdf)} reaches moved onto the D8 paths their lines drain along.")
    LOG.info(f"Kept where they were: {relocation.off_channel} whose paths the DEM shows no more channel along than their lines.")
    LOG.info(f"{relocation.shortcut} whose paths cut their meanders.")
    LOG.info(f"{relocation.stranded} whose paths are neither their river nor drain on to the next reach.")
    LOG.info(f"{len(relocation.trails)} moved reaches keep their lines below and {len(relocation.leads)} above where their paths don't reach the reaches they join.")
    LOG.info(f"{relocation.past_junction} of the former where their paths ran on past the next reach's start.")
    return relocation.result()


class _Relocation:
    def __init__(self, source_gdf, flowdir, filled, dem, geotransform, projection, id_col, ds_col, area_col, valid_mask,
                 settings):
        self.source_gdf, self.id_col, self.settings = source_gdf, id_col, settings
        self.gt = tuple(geotransform)
        self.flow = flow = FlowNetwork(flowdir, geotransform, projection, valid_mask)
        self.valid = flow.valid
        self.source = prepare_source_network(source_gdf, id_col, ds_col, None, area_col, flow)
        self.nrows, self.ncols = flow.nrows, flow.ncols
        self.dx, self.dy, self.R = float(np.median(flow.dx_m)), float(flow.dy_m), flow.cell_size_m
        # Not copied where it is NaN off the DEM already, as fill_and_flow_directions leaves it: it is only read
        Z = np.asarray(filled, np.float64)
        self.Z = Z if np.isnan(Z[~self.valid]).all() else np.where(self.valid, Z, np.nan)
        # A single-precision DEM is kept as it is rather than copied into double precision; what is worked out from it is
        # worked out in double precision all the same (see _channel_cells, _line_above)
        raw = np.asarray(dem)
        self.raw = raw if raw.dtype == np.float32 else np.asarray(raw, np.float64)
        self._turn_lone_reaches_downhill()
        self.inflow = self._with_inflow()
        self.F = flow.channel_area_km2.reshape(self.nrows, self.ncols)

    # --- coordinates: metres across the grid (x = column * dx, y = row * dy)

    def _metric(self, line) -> np.ndarray:
        xy = shapely.get_coordinates(line)
        return np.column_stack(((xy[:, 0] - self.gt[0]) / self.gt[1] * self.dx,
                                (xy[:, 1] - self.gt[3]) / self.gt[5] * self.dy))

    def _to_map(self, mxy: np.ndarray) -> np.ndarray:
        return np.column_stack((self.gt[0] + mxy[:, 0] / self.dx * self.gt[1],
                                self.gt[3] + mxy[:, 1] / self.dy * self.gt[5]))

    def _centres(self, cells: np.ndarray) -> np.ndarray:
        rows, cols = np.divmod(cells, self.ncols)
        return np.column_stack(((cols + 0.5) * self.dx, (rows + 0.5) * self.dy))

    def _densified(self, mxy: np.ndarray) -> np.ndarray:
        """``mxy`` with vertices at most a cell apart."""
        along = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(mxy, axis=0).T))))
        if along[-1] == 0:
            return mxy[:1]
        t = np.linspace(0, along[-1], max(int(np.ceil(along[-1] / self.R)), 1) + 1)
        return np.column_stack((np.interp(t, along, mxy[:, 0]), np.interp(t, along, mxy[:, 1])))

    def _on_dem(self, mxy: np.ndarray) -> np.ndarray:
        cols, rows = np.floor(mxy[:, 0] / self.dx).astype(np.int64), np.floor(mxy[:, 1] / self.dy).astype(np.int64)
        inside = (rows >= 0) & (rows < self.nrows) & (cols >= 0) & (cols < self.ncols)
        inside[inside] = self.valid[rows[inside], cols[inside]]
        return inside

    def _line(self, x: int) -> np.ndarray:
        """Reach x's line, densified, on the DEM."""
        h = self._densified(self._metric(self.source.geometry[x]))
        return h[self._on_dem(h)]

    def _cells(self, mxy: np.ndarray) -> np.ndarray:
        return np.floor(mxy[:, 1] / self.dy).astype(np.int64) * self.ncols + np.floor(mxy[:, 0] / self.dx).astype(np.int64)

    # --- setting up

    def _turn_lone_reaches_downhill(self) -> None:
        """A reach joined to no other runs to its end that is lower over its last quarter: the network can't say."""
        source = self.source
        joined = source.ds >= 0
        joined[source.ds[source.ds >= 0]] = True
        for r in np.flatnonzero(~joined):
            mxy = self._densified(self._metric(source.geometry[r]))
            inside = self._on_dem(mxy)
            z = np.full(len(mxy), np.nan)
            z[inside] = self.Z.ravel()[self._cells(mxy[inside])]
            quarter = max(len(z) // 4, 1)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                if np.nanmedian(z[:quarter]) < np.nanmedian(z[-quarter:]):
                    source.geometry[r] = shapely.reverse(source.geometry[r])

    def _with_inflow(self) -> np.ndarray:
        """
        What flows into each reach. The DEM's drainage, with what the source says flows in across the domain's edge
        added from where it enters (see _inflow_entries), becomes flow.channel_area_km2.
        """
        source, flow = self.source, self.flow
        n = source.ids.size
        inflow = np.zeros(n)
        np.add.at(inflow, source.ds[source.ds >= 0], source.area[source.ds >= 0])
        _, _, _, offsets, cell_of_sample, inside = _sample_lines(source.geometry, flow, flow.cell_size_m)
        in_domain = inside & flow.valid.ravel()[cell_of_sample]
        entering, entry_cells = _inflow_cells(offsets, in_domain, cell_of_sample, flow, self.Z.ravel())
        inflow[entering] = np.maximum(inflow[entering], source.area[entering])
        radius = np.array([self._radius(r) for r in range(n)])
        cells, km2 = _inflow_entries(entering, entry_cells, inflow, source, offsets, in_domain, cell_of_sample, flow,
                                     radius, self.settings)
        # The channel threshold doesn't matter here: only the drainage is used
        flow.build_channels(1.0, cells, km2)
        return inflow

    def _radius(self, reach: int) -> float:
        s = self.settings
        area = max(float(self.source.area[reach]), 0.0)
        return float(min(s.search_radius_m + s.search_radius_per_sqrt_km2 * np.sqrt(area), s.max_search_radius_m))

    # --- the paths, and which reaches move onto them

    def run(self) -> None:
        source, s = self.source, self.settings
        self.paths, self.off_dem = {}, {}
        area = np.maximum(source.area, self.inflow)
        for x in range(source.ids.size):
            h = self._densified(self._metric(source.geometry[x]))
            inside = self._on_dem(h)
            if inside.sum() < 2:
                continue

            on = np.flatnonzero(inside)
            if on[0] > 0 or on[-1] < len(h) - 1:  # its stretches off the DEM are kept as they are
                self.off_dem[x] = (h[:on[0]], h[on[-1] + 1:])
            path = self._path(h, inside, self._radius(x), float(area[x]) * s.area_factor,
                              float(self.inflow[x]) / s.area_factor)
            if path is not None and path.size >= 2:
                self.paths[x] = path

        self.off_channel = self.shortcut = 0
        voted = dict(self.paths)
        channel = self._channel_cells()
        for x in list(self.paths):
            line_cells = np.unique(self._cells(self._line(x)))
            on_path, on_line = channel[self.paths[x]].mean(), channel[line_cells].mean()
            if on_path < s.min_channel_share or (on_path < on_line + s.min_channel_gain
                                                 and not self._follows_a_bend(x, line_cells, channel)):
                del self.paths[x]
                self.off_channel += 1
            elif self._length_ratio(x) < s.min_length_ratio and self._line_above(x) < s.meander_max_rise_m:
                del self.paths[x]
                self.shortcut += 1

        # Each reach is carried down D8 from its end onto the line of the reach it flows into (or of that reach's main
        # upstream reach, beside which a tributary may join): its new line where it moved, else its own
        upstream = [[] for _ in range(source.ids.size)]
        for r in np.flatnonzero(source.ds >= 0):
            upstream[source.ds[r]].append(r)

        main = [max(ups, key=lambda u: source.area[u]) if ups else -1 for ups in upstream]
        def onto(x: int, d: int, end: int, river: bool = False) -> np.ndarray | None:
            """The D8 path from ``end`` onto reach d's line, or with ``river`` onto wherever its river may be: its
            path too, though it didn't move onto it."""
            reaches = [d] + ([main[d]] if main[d] >= 0 and main[d] != x else [])
            target = set()
            for y in reaches:
                target |= set(self._cells_of(y).tolist())
                if river and y in voted:
                    target |= set(voted[y].tolist())

            return _down_to(end, self.flow.down, target, int(3 * self._radius(x) / self.R) + 10)

        # A moved reach's path has to be its river and lead on down it: draining at least what the reach does over
        # area_factor, and ending within its search radius of the line's end, or draining on into the reach it flows
        # into -- carried down D8, it reaches that reach's line or path within the carrying limit; at an outlet,
        # either. Across a flat fan D8 can carry a river some other way, leaving a gully near the top of its line the
        # most voted for, or a river's top cells drain back off the far side of a plateau, down a path that runs from
        # the line's top away from it; down a braided river D8 sends the river down one branch, and the paths down the
        # others drain little but lead on down them. Else the reach keeps its line. A reach kept can strand the reaches
        # flowing into it, so this is repeated until none is.
        self.stranded = 0
        F = self.F.ravel()
        changed = True
        while changed:
            changed = False
            for x in list(self.paths):
                d = int(source.ds[x])
                h = self._line(x)
                river = F[self.paths[x]].max() >= area[x] / s.area_factor
                near_end = len(h) == 0 or \
                    np.hypot(*(self._centres(self.paths[x][-1:])[0] - h[-1])) <= self._radius(x)
                if d >= 0:
                    drains_on = (river and near_end) or onto(x, d, int(self.paths[x][-1]), river=True) is not None
                else:
                    drains_on = river or near_end

                if not drains_on:
                    del self.paths[x]
                    self.stranded += 1
                    changed = True

        # The reaches below first, so that a reach is carried onto where the next one is carried; a path that runs on
        # past the next reach's start ends near it instead, and keeps its line below there (see _to_junction)
        depth = self._depths()
        joined = set()
        self.past_junction = 0
        for x in sorted(self.paths, key=lambda r: (depth[r], -source.area[r])):
            d = int(source.ds[x])
            if d < 0:
                continue

            down = onto(x, d, int(self.paths[x][-1]))
            path = self.paths[x] if down is None else np.concatenate((self.paths[x], down))
            self.paths[x], joins, past = self._to_junction(d, path, down is not None)
            self.past_junction += past
            if joins:
                joined.add(x)

        # Then a kept line whose downstream reach moved is carried onto its new line. Moved reaches not carried on, and
        # kept ones that can't be, end off the reach they flow into
        self.tails = {}
        stranded_ends = set()
        for x in range(source.ids.size):
            d = int(source.ds[x])
            if d < 0 or (x in self.paths and (x in joined or len(self.off_dem.get(x, ((), ()))[1]))):
                continue

            if x in self.paths:
                stranded_ends.add(x)
                continue

            if d not in self.paths:
                continue

            h = self._densified(self._metric(source.geometry[x]))
            if len(h) < 2 or not self._on_dem(h[-1:])[0]:
                continue

            down = onto(x, d, int(self._cells(h[-1:])[0]))
            if down is None:
                stranded_ends.add(x)
            elif down.size:
                self.tails[x] = np.vstack((h, self._centres(down)))

        # Where neither reaches the other, the moved reaches keep their lines (see _keeping_lines)
        self._keeping_lines(stranded_ends)

    def _keeping_lines(self, stranded_ends: set) -> None:
        """
        Where a reach ends off the reach it flows into (``stranded_ends``), the moved ones keep their lines where their
        paths don't reach, so that the network stays joined where the lines met: a moved reach in ``stranded_ends``
        its line below where its path ends nearest it, and the reach it flows into, where that moved, its line above
        where its path starts nearest it; each joined to its path by a straight stretch.
        """
        source = self.source
        self.leads, self.trails = {}, {}
        for x in stranded_ends:
            if x in self.paths:
                h = self._line(x)
                end = self._centres(self.paths[x][-1:])[0]
                k = int(np.argmin(np.hypot(*(h - end).T)))
                self.trails[x] = np.vstack((self._densified(np.vstack((end, h[k])))[1:-1], h[k:]))

            d = int(source.ds[x])
            if d in self.paths and d not in self.leads and not len(self.off_dem.get(d, ((), ()))[0]):
                h = self._line(d)
                head = self._centres(self.paths[d][:1])[0]
                k = int(np.argmin(np.hypot(*(h - head).T)))
                self.leads[d] = np.vstack((h[:k + 1], self._densified(np.vstack((h[k], head)))[1:-1]))

    def _to_junction(self, d: int, path: np.ndarray, reached: bool) -> tuple[np.ndarray, bool, bool]:
        """
        A ``path`` carried down D8 onto the line of reach d, the reach it flows into, where ``reached``: ending where
        it first comes onto that line, or where it runs on past d's start (see RelocationSettings.junction_m). With
        whether it still ends on d (or on the reach flowing into d it was carried onto), and whether it ran on past
        d's start.
        """
        line = self._cells_of(d)
        if line.size == 0:
            return path, reached, False
        
        on_line = np.flatnonzero(np.isin(path, line))
        if on_line.size:
            path, reached = path[:max(int(on_line[0]), 1) + 1], True

        if reached and d in self.paths:
            return path, True, False  # two flow paths meet where they join
        
        gap = np.hypot(*(self._centres(path) - self._centres(line[:1])[0]).T)
        near = max(self.settings.junction_m, 1.5 * self.R)
        nearest = max(int(np.argmin(gap)), 1)
        if (reached and gap[-1] <= near) or gap.min() > near or nearest == path.size - 1:
            return path, reached, False
        
        return path[:nearest + 1], False, True

    def _depths(self) -> np.ndarray:
        """How many reaches each reach is above its network's outlet."""
        ds = self.source.ds
        depth = np.full(ds.size, -1, np.int64)
        for r in range(ds.size):
            chain, x = [], r
            while x >= 0 and depth[x] < 0 and len(chain) <= ds.size:
                chain.append(x)
                x = int(ds[x])

            below = depth[x] if x >= 0 else -1
            for k, y in enumerate(reversed(chain)):
                depth[y] = below + 1 + k

        return depth

    def _path(self, h: np.ndarray, inside: np.ndarray, radius: float, max_area: float,
              low_area: float) -> np.ndarray | None:
        """
        The D8 path the cells under line ``h`` drain along (see the module): within ``radius`` of it and draining at
        most ``max_area``, and below a reach others flow into, starting where it drains ``low_area``.
        """
        pad = radius + 3 * self.R
        r0, r1 = max(int((h[:, 1].min() - pad) / self.dy), 0), min(int((h[:, 1].max() + pad) / self.dy) + 1, self.nrows)
        c0, c1 = max(int((h[:, 0].min() - pad) / self.dx), 0), min(int((h[:, 0].max() + pad) / self.dx) + 1, self.ncols)
        wnr, wnc = r1 - r0, c1 - c0
        hr = np.floor(h[:, 1] / self.dy).astype(np.int64) - r0
        hc = np.floor(h[:, 0] / self.dx).astype(np.int64) - c0
        on = (hr >= 0) & (hr < wnr) & (hc >= 0) & (hc < wnc)
        free = np.ones((wnr, wnc), bool)
        free[hr[on], hc[on]] = False
        corridor = self.valid[r0:r1, c0:c1] & within_distance(~free, (self.dy, self.dx), radius)
        area = np.ascontiguousarray(self.F[r0:r1, c0:c1]).ravel()
        starts = np.unique((hr[on & inside] + r0) * self.ncols + (hc[on & inside] + c0)).astype(np.int64)
        votes = np.zeros(wnr * wnc, np.int32)
        _votes(starts, self.flow.down, self.ncols, r0, c0, wnr, wnc, corridor.ravel(), area, max_area,
               int(2 * len(h) + 4 * radius / self.R), votes)
        if votes.max() == 0:
            return None
        
        top = np.flatnonzero(votes >= self.settings.plateau * votes.max())
        rows, cols = np.divmod(top, wnc)
        last = h[np.flatnonzero(inside)[-1]]
        end = top[np.argmin(np.hypot((cols + c0 + 0.5) * self.dx - last[0], (rows + r0 + 0.5) * self.dy - last[1]))]
        path = _trunk(int(end), self.flow.down, self.ncols, r0, c0, wnr, wnc, votes, area)
        # Below a reach others flow into, the line's top cells drain down the valley side to the river, and that
        # isn't the river: the path starts where it drains what flows in, and is carried up the river from there. On
        # the DEM, that is no more than the river near the line's start drains: on a fan, the DEM can send some of what
        # the source says flows in down another channel, and the river's top would be cut off.
        if low_area > 0:
            first = h[np.flatnonzero(inside)[0]]
            wr, wc = np.mgrid[0:wnr, 0:wnc]
            near = self.valid[r0:r1, c0:c1] & (np.hypot((wc + c0 + 0.5) * self.dx - first[0],
                                                        (wr + r0 + 0.5) * self.dy - first[1]) <= radius)
            if near.any():
                low_area = min(low_area, float(area[near.ravel()].max()) / self.settings.area_factor)

            reaching = np.flatnonzero(area[path] >= low_area)
            if reaching.size:
                path = path[reaching[0]:]

        rows, cols = np.divmod(path, wnc)
        cells = (rows + r0) * self.ncols + (cols + c0)
        if low_area > 0:
            cells = self._carried_up(cells, h[np.flatnonzero(inside)[0]], low_area)

        return cells

    def _carried_up(self, cells: np.ndarray, start: np.ndarray, low_area: float) -> np.ndarray:
        """``cells`` carried upstream from their head along the neighbour draining most (at least ``low_area``), while
        that comes nearer the line's ``start``."""
        head, extra, seen = int(cells[0]), [], set(cells.tolist())
        nearest = np.hypot(*(self._centres(np.array([head]))[0] - start))
        F = self.F.ravel()
        while True:
            row, col = divmod(head, self.ncols)
            best, most = -1, low_area
            for d_row in (-1, 0, 1):
                for d_col in (-1, 0, 1):
                    r, c = row + d_row, col + d_col
                    if (d_row or d_col) and 0 <= r < self.nrows and 0 <= c < self.ncols:
                        u = r * self.ncols + c
                        if self.flow.down[u] == head and F[u] >= most and u not in seen:
                            best, most = u, F[u]

            if best < 0:
                break

            gap = np.hypot(*(self._centres(np.array([best]))[0] - start))
            if gap >= nearest:
                break

            nearest, head = gap, best
            extra.append(best)
            seen.add(best)

        return np.concatenate((np.asarray(extra[::-1], np.int64), cells)) if extra else cells

    def _channel_cells(self) -> np.ndarray:
        """A flat mask of the cells the DEM shows a channel on (see RelocationSettings.min_channel_share)."""
        s, raw = self.settings, self.raw
        valid = self.valid & np.isfinite(raw)
        # The water the DEM flattens near the lines' level, which needn't touch a line that is off its river, so as far
        # from a line as a river is looked for, whatever its width. Not the low ground by the lines as well: measured
        # from the lines being judged, it would count a line as on its channel
        lines = np.zeros(raw.size, np.int32)
        for x in range(self.source.ids.size):
            lines[self._cells(self._line(x))] = x + 1

        areas = {x + 1: float(self.source.area[x]) for x in range(self.source.ids.size)}
        water = river_channel(raw, valid, lines.reshape(raw.shape), areas, self.dx, self.dy,
                              ChannelSettings(corridor_m=s.max_search_radius_m, flat_widths=None), connected=False,
                              channel=False, double=True).flat_water
        del lines

        return (water | _incised(raw, self.Z, valid, s.channel_reach_cells, s.channel_depth_m)).ravel()

    def _follows_a_bend(self, x: int, line_cells: np.ndarray, channel: np.ndarray) -> bool:
        """Whether reach x's path follows a bend its line cuts across (see RelocationSettings.bend_channel_gain): longer
        than the line, and where they are more than 1.5 cells apart, along clearly more channel."""
        s = self.settings
        if s.bend_channel_gain is None or len(line_cells) == 0 or self._length_ratio(x) < s.bend_length_ratio:
            return False
        
        path_cells = np.unique(self.paths[x])
        line_xy, path_xy = self._centres(line_cells), self._centres(path_cells)
        line_off = line_cells[cKDTree(path_xy).query(line_xy)[0] > 1.5 * self.R]
        path_off = path_cells[cKDTree(line_xy).query(path_xy)[0] > 1.5 * self.R]
        if len(line_off) < 5 or len(path_off) < 5:
            return False
        
        return channel[path_off].mean() >= channel[line_off].mean() + s.bend_channel_gain

    def _length_ratio(self, x: int) -> float:
        """Reach x's path length over the length of the stretch of its line between where the path's ends are nearest
        it (inf where that stretch is under a few cells)."""
        h = self._line(x)
        if len(h) < 2:
            return np.inf
        
        line = shapely.LineString(h)
        centres = self._centres(self.paths[x])
        stretch = abs(line.project(shapely.Point(centres[-1])) - line.project(shapely.Point(centres[0])))
        if stretch < 3 * self.R:
            return np.inf
        
        return float(np.hypot(*np.diff(centres, axis=0).T).sum()) / stretch

    def _line_above(self, x: int) -> float:
        """How far reach x's line runs above its path on the DEM before filling: the median, over the line's vertices,
        of their height above the nearest path cell."""
        h = self._line(x)
        if len(h) == 0:
            return 0.0
        
        raw = self.raw.ravel()
        nearest = cKDTree(self._centres(self.paths[x])).query(h)[1]
        above = raw[self._cells(h)].astype(np.float64) - raw[self.paths[x][nearest]]
        return float(np.nanmedian(above)) if np.isfinite(above).any() else 0.0

    def _cells_of(self, x: int) -> np.ndarray:
        """The cells under reach x's new line, or its own where it keeps it."""
        return self.paths[x] if x in self.paths else self._cells(self._line(x))

    def result(self) -> gpd.GeoDataFrame:
        out = self.source_gdf.copy()
        position = {int(i): x for x, i in enumerate(self.source.ids)}
        ids = pd.to_numeric(out[self.id_col], errors="coerce").to_numpy()
        geometry = out.geometry.to_numpy().copy()
        for row, rid in enumerate(ids):
            x = position.get(int(rid)) if np.isfinite(rid) else None
            if x is None or (x not in self.paths and x not in self.tails):
                continue

            if x in self.tails:  # kept, and carried onto the moved reach it flows into
                points = self.tails[x]
            else:
                points = np.vstack((self.leads.get(x, np.empty((0, 2))), self._centres(self.paths[x]),
                                    self.trails.get(x, np.empty((0, 2)))))
                if x in self.off_dem:
                    above, below = self.off_dem[x]
                    points = np.vstack((above, points, below))

            xy = self._to_map(points)
            # Drawn as the source draws it: downstream end first where the source's first vertex is nearer the end its
            # line runs down to (see prepare_source_network). Not judged from the new line's ends: a path can end
            # farther from the line's downstream end than it starts
            first = shapely.get_coordinates(shapely.force_2d(geometry[row]))[0]
            ends = shapely.get_coordinates(self.source.geometry[x])[[0, -1]]
            if np.hypot(*(first - ends[1])) < np.hypot(*(first - ends[0])):
                xy = xy[::-1]

            geometry[row] = shapely.LineString(xy)

        out[out.geometry.name] = geometry
        return out


@njit(cache=True)
def _incised(raw: np.ndarray, filled: np.ndarray, valid: np.ndarray, k: int, depth: float) -> np.ndarray:
    """
    The ``valid`` cells of ``raw`` at least ``depth`` below both the cells ``k`` away along a row, a column or a
    diagonal (a cell off the grid or off ``valid`` counting as infinitely high), but for those the fill raised:
    ``filled`` more than 5 cm above ``raw``.
    """
    nrows, ncols = raw.shape
    out = np.zeros((nrows, ncols), np.bool_)
    for r in range(nrows):
        for c in range(ncols):
            if not valid[r, c] or filled[r, c] - raw[r, c] > 0.05:
                continue

            for d_row, d_col in ((0, 1), (1, 0), (1, 1), (1, -1)):
                ahead, behind = np.inf, np.inf
                r1, c1, r2, c2 = r + d_row * k, c + d_col * k, r - d_row * k, c - d_col * k
                if 0 <= r1 < nrows and 0 <= c1 < ncols and valid[r1, c1]:
                    ahead = raw[r1, c1]

                if 0 <= r2 < nrows and 0 <= c2 < ncols and valid[r2, c2]:
                    behind = raw[r2, c2]

                if raw[r, c] <= min(ahead, behind) - depth:
                    out[r, c] = True
                    break

    return out


@njit(cache=True)
def _votes(starts, down, ncols, r0, c0, wnr, wnc, corridor, area, max_area, max_steps, votes):
    """Add a vote to every window cell on the D8 path from each of ``starts``, while it is in the ``corridor`` and
    drains at most ``max_area``."""
    for q in range(starts.size):
        cell = starts[q]
        for _ in range(max_steps):
            row = cell // ncols
            wr, wc = row - r0, cell - row * ncols - c0
            if wr < 0 or wr >= wnr or wc < 0 or wc >= wnc:
                break

            w = wr * wnc + wc
            if not corridor[w] or area[w] > max_area:
                break
            
            votes[w] += 1
            if down[cell] < 0:
                break

            cell = down[cell]


@njit(cache=True)
def _trunk(start, down, ncols, r0, c0, wnr, wnc, votes, area):
    """From window cell ``start`` upstream through the neighbour flowing into it with the most votes (then the most
    drainage), while one has a vote; upstream to downstream."""
    path = np.empty(wnr * wnc, np.int64)
    n = 0
    w = start
    while n < path.size:
        path[n] = w
        n += 1
        wr, wc = w // wnc, w % wnc
        cell = (wr + r0) * ncols + (wc + c0)
        best, best_votes, best_area = -1, 0, -1.0
        for d_row in range(-1, 2):
            for d_col in range(-1, 2):
                r, c = wr + d_row, wc + d_col
                if (d_row == 0 and d_col == 0) or r < 0 or r >= wnr or c < 0 or c >= wnc:
                    continue

                u = r * wnc + c
                if down[(r + r0) * ncols + (c + c0)] != cell or votes[u] == 0:
                    continue

                if votes[u] > best_votes or (votes[u] == best_votes and area[u] > best_area):
                    best, best_votes, best_area = u, votes[u], area[u]

        if best < 0:
            break

        w = best

    return path[:n][::-1].copy()


def _down_to(cell: int, down: np.ndarray, target: set, max_steps: int) -> np.ndarray | None:
    """The D8 path below ``cell`` to the first cell in ``target`` (empty if ``cell`` is in it); None if it doesn't get
    there within ``max_steps``."""
    path = []
    for _ in range(max_steps):
        if cell in target:
            return np.asarray(path, np.int64)
        
        cell = int(down[cell])
        if cell < 0:
            break

        path.append(cell)

    return None


def breach_dammed_rivers(
        source_gdf: gpd.GeoDataFrame,
        dem: np.ndarray,
        geotransform: tuple,
        projection: str,
        id_col: str,
        ds_col: str,
        valid_mask: np.ndarray | None = None,
        settings: RelocationSettings | None = None,
) -> tuple[np.ndarray | None, list]:
    """
    ``dem`` with the lines of ``source_gdf`` burned through the places it dams their river, and the ids of the
    reaches whose lines were; None and no ids where it dams none.

    A DEM dams a river where it can't resolve the channel through something, as a gorge narrower than a cell: filled,
    the valley above floods up to its lowest outlet, which can be some other way than down the river, and then no
    flow path below drains the river. Followed downstream, the DEM under the line rises from a low to dam_height_m or
    more above it, and comes back down to it, within max_dam_reaches reaches. At both lows the line runs on level, the
    cell above the first and the cell below the second within dam_end_step_m: so the burn starts on the river above
    the dam, not out of a pit, and ends on it below, past any hump left at the dam's foot. Unless the DEM has a way
    between the two lows that climbs less than dam_height_m -- the river may run there, off its line -- the line is
    burned from one low to the other, falling evenly between them.
    """
    settings = settings or RelocationSettings()
    if source_gdf.empty:
        return None, []
    
    z = np.asarray(dem, np.float64)
    valid = np.isfinite(z) if valid_mask is None else np.asarray(valid_mask, np.bool_) & np.isfinite(z)
    # No flow paths yet: the lines are turned to run downstream by the network alone, or by their heights
    flow = FlowNetwork.without_flow(z.shape, geotransform, projection, valid)
    source = prepare_source_network(source_gdf, id_col, ds_col, None, None, flow)
    if source.ids.size == 0:
        return None, []
    
    s_rows, s_cols, _, offsets, cell_of_sample, inside = _sample_lines(source.geometry, flow, flow.cell_size_m)
    z = z.ravel()
    in_domain = inside & flow.valid.ravel()[cell_of_sample]
    order = _downhill_where_alone(np.where(in_domain, z[cell_of_sample], np.nan), offsets, source.ds)
    s_rows, s_cols, cell_of_sample, in_domain = s_rows[order], s_cols[order], cell_of_sample[order], in_domain[order]
    heights = np.where(in_domain, z[cell_of_sample], np.nan)

    reach, low, end_reach, end, _ = _rises_and_falls(heights, cell_of_sample, offsets, source.ds, in_domain,
                                                        settings.max_dam_reaches, settings.dam_height_m,
                                                        settings.dam_end_step_m)
    if reach.size == 0:
        return None, []
    
    ground = np.where(flow.valid.ravel(), z, np.inf)
    seen = np.zeros(z.size, np.bool_)
    dx_m = float(np.median(flow.dx_m))
    levels = {}  # burned cell -> level
    burned = set()
    for r, a, x, b in zip(reach.tolist(), low.tolist(), end_reach.tolist(), end.tolist()):
        start, goal = int(cell_of_sample[a]), int(cell_of_sample[b])
        if _pass_height(ground, flow.nrows, flow.ncols, start, goal, heights[a] + settings.dam_height_m,
                        seen) < heights[a] + settings.dam_height_m:
            continue  # a lower way round

        samples, y = [], r
        while True:
            samples.append(np.arange(a, b + 1 if y == x else offsets[y + 1]))
            if y == x:
                break

            y = int(source.ds[y])
            a = int(offsets[y])

        samples = np.concatenate(samples)
        along = np.concatenate(([0.0], np.cumsum(np.hypot(np.diff(s_rows[samples]) * flow.dy_m,
                                                          np.diff(s_cols[samples]) * dx_m))))
        level = heights[samples[0]] + (heights[samples[-1]] - heights[samples[0]]) * along / max(along[-1], 1e-9)
        at = {}
        for cell, height in zip(cell_of_sample[samples].tolist(), level.tolist()):
            at[cell] = min(at.get(cell, np.inf), height)

        below = np.inf
        for cell in reversed(_eight_connected(cell_of_sample[samples].tolist(), flow.ncols)):
            below = at.get(cell, below)  # a cell filled in between two takes the lower one's level
            levels[cell] = min(levels.get(cell, np.inf), below)

        burned.add(int(source.ids[r]))

    if not levels:
        return None, []
    
    out = np.array(dem, copy=True)
    cells = np.fromiter(levels, np.int64, len(levels))
    flat = out.reshape(-1)
    level = np.fromiter(levels.values(), np.float64, len(levels))
    cut = flat[cells] - level
    flat[cells] = np.minimum(flat[cells], level)
    LOG.info(f"Relocation: the DEM dams the river under {len(burned)} reaches, up to {cut.max():.0f} m high with no "
             f"way round {settings.dam_height_m:g} m lower; their lines were burned through {int((cut > 0).sum())} "
             f"cells to find their flow paths: {sorted(burned)}")
    
    return out, sorted(burned)


def _downhill_where_alone(heights: np.ndarray, offsets: np.ndarray, ds: np.ndarray) -> np.ndarray:
    """
    The order of the samples with the lines of reaches joined to no other turned to run downhill: the network turns
    the rest. Each such line runs to its end that is lower over its last quarter.
    """
    order = np.arange(heights.size)
    joined = ds >= 0
    joined[ds[ds >= 0]] = True
    for r in np.flatnonzero(~joined):
        a, b = offsets[r], offsets[r + 1]
        quarter = max((b - a) // 4, 1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            if np.nanmedian(heights[a:a + quarter]) < np.nanmedian(heights[b - quarter:b]):
                order[a:b] = order[a:b][::-1]

    return order


@njit(cache=True)
def _rises_and_falls(heights: np.ndarray, cells: np.ndarray, offsets: np.ndarray, ds: np.ndarray, ok: np.ndarray,
                     max_reaches: int, min_rise: float, max_step: float) -> tuple:
    """
    Where the ``heights`` under the lines rise ``min_rise`` or more and come back down: from a low, down its reach
    and the reaches below (``max_reaches`` in all, through samples that are ``ok``), to the first sample no higher
    whose next cell down the lines is within ``max_step`` of it. A low has the cell before it up its reach within
    ``max_step`` of it too: at both ends the line runs on, not out of a pit or over the lip of a drop. Each is given
    by the reach and sample it starts at, the reach and sample it ends at, and how high it rises. Down each reach,
    the first such low is taken, and the next looked for past where it comes back down.
    """
    n = offsets.size - 1
    out = np.empty((heights.size, 5), np.float64)
    m = 0
    for r in range(n):
        i = offsets[r]
        last = offsets[r + 1]
        while i < last:
            if not ok[i]:
                i += 1
                continue

            low = heights[i]
            before = i - 1
            while before >= offsets[r] and cells[before] == cells[i]:
                before -= 1

            if before < offsets[r] or not ok[before] or abs(heights[before] - low) > max_step:
                i += 1
                continue

            x, s, count, peak, found = r, i + 1, 1, low, False
            while True:
                if s >= offsets[x + 1]:
                    x = ds[x]
                    if x < 0 or count >= max_reaches:
                        break

                    count += 1
                    s = offsets[x]
                    continue

                if not ok[s]:
                    break

                if heights[s] <= low:
                    after = _next_cell(cells, offsets, ds, ok, x, s)
                    if after >= 0 and abs(heights[after] - heights[s]) <= max_step:
                        found = True
                        break

                peak = max(peak, heights[s])
                s += 1

            if found and peak - low >= min_rise:
                out[m, 0], out[m, 1], out[m, 2], out[m, 3], out[m, 4] = r, i, x, s, peak - low
                m += 1
                i = s if x == r else last
            else:
                i += 1

    out = out[:m]
    return (out[:, 0].astype(np.int64), out[:, 1].astype(np.int64), out[:, 2].astype(np.int64),
            out[:, 3].astype(np.int64), out[:, 4])


@njit(cache=True)
def _next_cell(cells: np.ndarray, offsets: np.ndarray, ds: np.ndarray, ok: np.ndarray, x: int, s: int) -> int:
    """The first sample after sample ``s`` (of reach ``x``) down the lines on another cell; -1 if they leave the DEM
    or end first."""
    cell = cells[s]
    s += 1
    while True:
        if s >= offsets[x + 1]:
            x = ds[x]
            if x < 0:
                return -1
            
            s = offsets[x]
            continue

        if not ok[s]:
            return -1
        
        if cells[s] != cell:
            return s
        
        s += 1


@njit(cache=True)
def _pass_height(ground: np.ndarray, nrows: int, ncols: int, start: int, goal: int, cap: float,
                 seen: np.ndarray) -> float:
    """
    The lowest height a way from ``start`` to ``goal`` across 8-connected cells of ``ground`` (inf off the DEM) has
    to climb to, or ``cap`` if none stays below it. ``seen`` is all False, and is left so.
    """
    heap = [(ground[start], start)]
    visited = [start]
    seen[start] = True
    result = cap
    while len(heap) > 0:
        height, cell = heapq.heappop(heap)
        if height >= cap:
            break

        if cell == goal:
            result = height
            break

        row = cell // ncols
        col = cell - row * ncols
        for d_row in range(-1, 2):
            for d_col in range(-1, 2):
                r, c = row + d_row, col + d_col
                if (d_row == 0 and d_col == 0) or r < 0 or r >= nrows or c < 0 or c >= ncols:
                    continue

                neighbour = r * ncols + c
                if seen[neighbour] or ground[neighbour] == np.inf:
                    continue

                seen[neighbour] = True
                visited.append(neighbour)
                heapq.heappush(heap, (max(height, ground[neighbour]), neighbour))

    for cell in visited:
        seen[cell] = False

    return result


def _eight_connected(cells: list, ncols: int) -> list:
    """``cells`` with any gaps between consecutive ones filled, so that each touches the next."""
    out = []
    for cell in cells:
        if out:
            r0, c0 = divmod(out[-1], ncols)
            r1, c1 = divmod(cell, ncols)
            steps = max(abs(r1 - r0), abs(c1 - c0))
            for k in range(1, steps):
                out.append(round(r0 + (r1 - r0) * k / steps) * ncols + round(c0 + (c1 - c0) * k / steps))

        out.append(cell)

    return out


def _inflow_entries(entering: np.ndarray, entry_cells: np.ndarray, inflow: np.ndarray, source: SourceNetwork,
                    offsets: np.ndarray, in_domain: np.ndarray, cell_of_sample: np.ndarray, flow: FlowNetwork,
                    radius_m: np.ndarray, settings: RelocationSettings) -> tuple[np.ndarray, np.ndarray]:
    """
    The cells where the water of the rivers flowing in across the domain's edge (``entering``, with what they
    drain in ``inflow``) is added to the D8 network, and how much (km2) at each.

    Each takes its water in at its cell of ``entry_cells`` (see _inflow_cells), unless that cell's D8 path leaves
    the domain beside the river before following it min_inflow_run_m. A river flowing in on a flat (FABDEM
    flattens water) has the flat's upstream end drain back out across the edge it is level with, and a river along
    the edge can leave the domain and come back in. The water then goes in further down the river's course -- the
    reach and the reaches below, up to one entering on its own -- at the first cell whose path follows it that far,
    and again past wherever that path leaves the domain beside the course. Each time it has left the domain, so
    none of it is counted twice.
    """
    cells, km2 = [], []
    if entering.size == 0:
        return np.asarray(cells, np.int64), np.asarray(km2, np.float64)
    
    run = max(10, round(settings.min_inflow_run_m / flow.cell_size_m))
    max_cells = int(settings.max_course_m / flow.cell_size_m)
    own_inflow = np.zeros(source.ids.size, bool)
    own_inflow[entering] = True
    for r, first in zip(entering.tolist(), entry_cells.tolist()):
        course = _course(r, offsets, in_domain, cell_of_sample, source.ds, own_inflow, max_cells)
        at = np.flatnonzero(course == first)
        if course.size and at.size == 0:
            # It comes in at its river, a cell or two beside where its line crosses the edge (see _river_at_edge)
            row, col = divmod(first, flow.ncols)
            rows, cols = np.divmod(course, flow.ncols)
            near = np.flatnonzero(np.maximum(np.abs(rows - row), np.abs(cols - col)) <= 2)
            if near.size:
                course = np.insert(course, near[0], first)
                at = near[:1]

        if course.size == 0 or at.size == 0:
            cells.append(first)
            km2.append(inflow[r])
            continue

        tree = cKDTree(np.column_stack(np.divmod(course, flow.ncols)))
        beside = radius_m[r] / flow.cell_size_m
        end = course.size - 1 - run  # a path this far down the course has followed it out of the domain
        added = []
        k = int(at[0])
        while k < course.size:
            for j in range(k, course.size):
                path, ended = _walk(flow.down, int(course[j]), 4 * (course.size - j) + 100)
                distance, index = tree.query(np.column_stack(np.divmod(path, flow.ncols)),
                                             distance_upper_bound=beside)
                reached = max(j, int(index[distance <= beside].max(initial=j)))
                gone = ended and distance[-1] <= beside and reached < end  # out of the domain beside the course
                if not (gone and reached < j + run):
                    break
            else:
                break

            added.append(int(course[j]))
            if not gone:
                break

            k = reached + 1

        if not added:
            added = [first]

        cells += added
        km2 += [inflow[r]] * len(added)

    return np.asarray(cells, np.int64), np.asarray(km2, np.float64)


def _course(r: int, offsets: np.ndarray, in_domain: np.ndarray, cell_of_sample: np.ndarray, ds: np.ndarray,
            stop: np.ndarray, max_cells: int) -> np.ndarray:
    """
    The cells of reach r's line in the domain, and then of the reaches below it until one in ``stop``, up to
    ``max_cells``: a river's course across the domain, skipping where it is outside it.
    """
    course = []
    x = r
    while x >= 0 and len(course) < max_cells:
        samples = np.arange(offsets[x], offsets[x + 1])
        for cell in cell_of_sample[samples[in_domain[samples]]].tolist():
            if not course or course[-1] != cell:
                course.append(cell)

        x = int(ds[x])
        if x >= 0 and stop[x]:
            break
        
    return np.asarray(course[:max_cells], np.int64)


@njit(cache=True)
def _walk(down: np.ndarray, start: int, max_steps: int) -> tuple:
    """The D8 path from ``start``, at most ``max_steps`` cells on, and whether it ends (leaves the domain) there."""
    out = np.empty(max_steps + 1, np.int64)
    cell = start
    for n in range(max_steps + 1):
        out[n] = cell
        cell = down[cell]
        if cell < 0:
            return out[:n + 1], True
    return out, False
