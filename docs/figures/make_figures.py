"""
Make the figures of the documentation (the PNG files beside this script).

    python docs/figures/make_figures.py --run <run folder> --dem <N39W109_FABDEM_V1-2.tif>

stream_moving.png is a small synthetic example, moved by NenCarta, and needs neither argument. The others are
drawn from a run on the FABDEM tile N39W109 (the Colorado River near Loma, Colorado) with GEOGLOWS streams in
return_period mode, with burn_streams, move_stream_network_to_thalweg, relocate_misplaced_streams and
drop_multilinestrings, use_specified_depth_for_bathy_mask off, and the Curve2Flood-FLDPLNpy mapper. ``--run`` is
that run's folder and ``--dem`` the FABDEM tile it was run on.
"""
import argparse
from collections import deque
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LightSource
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from osgeo import gdal, osr
from shapely.geometry import LineString

from nencarta.tasks.stream_conflation import (
    D8_OFFSETS,
    ConflationSettings,
    conflate_to_flow_directions,
)

gdal.UseExceptions()
HERE = Path(__file__).resolve().parent
plt.rcParams.update({"font.size": 9, "axes.titlesize": 9, "legend.fontsize": 8, "savefig.dpi": 150})

WATER = "#a6cee3"
SOURCE = "#d7301f"
MOVED = "#08306b"
CREDIT = "Elevation: FABDEM. Land cover: ESA WorldCover 2021. Streams: GEOGLOWS."

# The map window, in degrees: the Colorado River leaving the Grand Valley for Horsethief Canyon
WINDOW = (-108.875, 39.152, -108.745, 39.193)
# The cross section of bathymetry_cross_section.png, from south to north
SECTION_X, SECTION_Y = -108.792, (39.1565, 39.1765)


def save(fig, name: str) -> None:
    path = HERE / name
    fig.savefig(path, bbox_inches="tight", pil_kwargs={"optimize": True})
    plt.close(fig)
    print(f"Wrote {path}")


# --- A synthetic example of moving streams -----------------------------------------------------------------------

CELL = 30.0
CODE = {offset: code for code, offset in D8_OFFSETS.items()}


def _path(*corners):
    """The D8 cells of straight runs between ``corners`` (row, col), each corner once."""
    cells = [corners[0]]
    for r1, c1 in corners[1:]:
        r, c = cells[-1]
        while (r, c) != (r1, c1):
            r += (r1 > r) - (r1 < r)
            c += (c1 > c) - (c1 < c)
            cells.append((r, c))
    return cells


def _flowdir(shape, paths, exits):
    """D8 pointers: along ``paths``, and from every other cell by the shortest route to the nearest path cell."""
    flowdir = np.zeros(shape, np.int16)
    on_path = np.zeros(shape, bool)
    for path in paths:
        for (r0, c0), (r1, c1) in zip(path[:-1], path[1:]):
            flowdir[r0, c0] = CODE[(r1 - r0, c1 - c0)]
        for cell in path:
            on_path[cell] = True
    for cell, offset in exits.items():
        flowdir[cell] = CODE[offset]
    queue, seen = deque(zip(*np.nonzero(on_path))), on_path.copy()
    while queue:
        r, c = queue.popleft()
        for dr, dc in CODE:
            nr, nc = r - dr, c - dc
            if 0 <= nr < shape[0] and 0 <= nc < shape[1] and not seen[nr, nc]:
                seen[nr, nc] = True
                flowdir[nr, nc] = CODE[(dr, dc)]
                queue.append((nr, nc))
    return flowdir


def _drifting_line(points, amplitude, waves, phase=0.0):
    """A smooth line through ``points`` (x, y), pushed sideways by up to ``amplitude`` cells."""
    xy = np.asarray(points, float)
    seg = np.r_[0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    t = np.linspace(0, seg[-1], 200)
    x, y = np.interp(t, seg, xy[:, 0]), np.interp(t, seg, xy[:, 1])
    kernel = np.ones(25) / 25
    x = np.convolve(np.pad(x, 12, mode="edge"), kernel, "valid")
    y = np.convolve(np.pad(y, 12, mode="edge"), kernel, "valid")
    dx, dy = np.gradient(x), np.gradient(y)
    norm = np.hypot(dx, dy)
    shift = amplitude * CELL * np.sin(2 * np.pi * waves * t / t[-1] + phase)
    return LineString(np.column_stack((x - dy / norm * shift, y + dx / norm * shift)))


def stream_moving_figure() -> None:
    shape = (11, 26)
    main = _path((5, 0), (5, 4), (7, 6), (7, 10), (9, 12), (9, 18), (8, 19), (8, 25))
    tributary = _path((0, 18), (3, 18), (5, 16), (8, 16), (9, 16))  # joins the main river at (9, 16)
    flowdir = _flowdir(shape, [main, tributary], {(8, 25): (0, 1)})

    def xy(cell):
        r, c = cell
        return (c + 0.5) * CELL, -(r + 0.5) * CELL

    # The source network joins them three cells further down than the DEM does
    junction = xy((9, 19))
    upper = _drifting_line([xy(c) for c in main[:main.index((9, 18)) + 1]] + [junction], 1.1, 1.5)
    lower = _drifting_line([junction] + [xy(c) for c in main[main.index((8, 19)):]], 0.9, 1.0, np.pi)
    trib = _drifting_line([xy(c) for c in tributary[:-1]] + [junction], 1.0, 1.0, np.pi / 2)
    source = gpd.GeoDataFrame({"LINKNO": [1, 2, 3], "DSLINKNO": [3, 3, -1], "order": [3, 1, 3]},
                              geometry=[upper, trib, lower])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32613)
    source = source.set_crs(srs.ExportToWkt())
    result = conflate_to_flow_directions(source, flowdir, (0.0, CELL, 0.0, 0.0, 0.0, -CELL), srs.ExportToWkt(),
                                         "LINKNO", "DSLINKNO", order_col="order",
                                         settings=ConflationSettings(min_channel_area_km2=0.005))

    colors = {1: "#2171b5", 2: "#e6550d", 3: "#31a354"}
    fig, ax = plt.subplots(figsize=(8, 4.6))
    ext = (0, shape[1] * CELL, -shape[0] * CELL, 0)
    for k in range(shape[1] + 1):
        ax.axvline(k * CELL, color="0.88", lw=0.5, zorder=0)
    for k in range(shape[0] + 1):
        ax.axhline(-k * CELL, color="0.88", lw=0.5, zorder=0)
    for reach, color in colors.items():
        cells = np.ma.masked_where(result.raster != reach, np.ones(shape))
        ax.imshow(cells, extent=ext, cmap=matplotlib.colors.ListedColormap([color]), alpha=0.35,
                  interpolation="nearest", zorder=1)
    for path in (main, tributary):
        for cell in path:
            code = int(flowdir[cell])
            if code:
                dr, dc = D8_OFFSETS[code]
                x, y = xy(cell)
                ax.annotate("", xy=(x + 0.42 * dc * CELL, y - 0.42 * dr * CELL),
                            xytext=(x - 0.3 * dc * CELL, y + 0.3 * dr * CELL),
                            arrowprops=dict(arrowstyle="-|>", color="0.35", lw=0.7, mutation_scale=7), zorder=2)
    for reach, line in zip(source.LINKNO, source.geometry):
        ax.plot(*line.xy, color=colors[reach], lw=1.8, ls=(0, (4, 2)), zorder=3)
    ax.plot(*junction, "o", mfc="white", mec="k", ms=7, zorder=4)
    ax.plot(*xy((9, 16)), "o", color="k", ms=6, zorder=4)
    ax.set_xlim(ext[0], ext[1])
    ax.set_ylim(ext[2], ext[3])
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_color("0.7")
    ax.legend(handles=[
        Line2D([], [], color="0.3", lw=1.8, ls=(0, (4, 2)), label="Source line"),
        Patch(facecolor="0.6", alpha=0.5, label="Moved reach (D8 cells)"),
        Line2D([], [], color="0.35", marker=r"$\rightarrow$", ls="none", ms=9, label="D8 flow direction"),
        Line2D([], [], marker="o", mfc="white", mec="k", ls="none", label="Confluence in the source network"),
        Line2D([], [], marker="o", color="k", ls="none", label="Where the flow paths meet"),
    ], loc="lower left", bbox_to_anchor=(0, 1.01), ncol=3, frameon=False, handlelength=2.2)
    save(fig, "stream_moving.png")


# --- Figures from the N39W109 run ---------------------------------------------------------------------------------

class Run:
    """The rasters and stream networks of the example run, in the window."""

    def __init__(self, folder: Path, dem: Path):
        self.folder = Path(folder)
        self.dem_path = Path(dem)
        ds = gdal.Open(str(self.dem_path))
        gt = ds.GetGeoTransform()
        x0, y0, x1, y1 = WINDOW
        self.c0, self.r0 = int((x0 - gt[0]) / gt[1]), int((y1 - gt[3]) / gt[5])
        self.c1, self.r1 = int((x1 - gt[0]) / gt[1]), int((y0 - gt[3]) / gt[5])
        self.gt = gt
        self.extent = (gt[0] + self.c0 * gt[1], gt[0] + self.c1 * gt[1], gt[3] + self.r1 * gt[5],
                       gt[3] + self.r0 * gt[5])
        self.lat = (y0 + y1) / 2
        self.dx_m = gt[1] * 111_320 * np.cos(np.radians(self.lat))
        self.dy_m = -gt[5] * 110_574

    def find(self, pattern: str) -> Path:
        matches = sorted(self.folder.rglob(pattern))
        if not matches:
            raise FileNotFoundError(f"No {pattern} in {self.folder}")
        return matches[0]

    def read(self, path: Path) -> np.ndarray:
        ds = gdal.Open(str(path))
        array = ds.ReadAsArray(self.c0, self.r0, self.c1 - self.c0, self.r1 - self.r0).astype(float)
        nodata = ds.GetRasterBand(1).GetNoDataValue()
        if nodata is not None:
            array[array == nodata] = np.nan
        return array

    def streams(self, pattern: str) -> gpd.GeoDataFrame:
        x0, y0, x1, y1 = WINDOW
        return gpd.read_parquet(self.find(pattern)).to_crs(4326).cx[x0:x1, y0:y1]

    def base_map(self, ax, dem: np.ndarray, water: np.ndarray) -> None:
        hillshade = LightSource(315, 45).hillshade(dem, vert_exag=3, dx=self.dx_m, dy=self.dy_m)
        ax.imshow(hillshade, cmap="gray", vmin=-0.2, vmax=1.2, extent=self.extent, interpolation="bilinear")
        ax.imshow(np.ma.masked_where(~water, water), cmap=matplotlib.colors.ListedColormap([WATER]),
                  extent=self.extent, interpolation="nearest")
        ax.set_xlim(self.extent[0], self.extent[1])
        ax.set_ylim(self.extent[2], self.extent[3])
        ax.set_aspect(1 / np.cos(np.radians(self.lat)))
        ax.set_xticks([])
        ax.set_yticks([])

    def scale_bar(self, ax, km: float = 1.0) -> None:
        width = km * 1000 / (111_320 * np.cos(np.radians(self.lat)))
        x, y = self.extent[0] + 0.004, self.extent[2] + 0.0025
        ax.plot([x, x + width], [y, y], color="k", lw=3, solid_capstyle="butt")
        ax.plot([x, x + width], [y, y], color="white", lw=1.5, solid_capstyle="butt")
        ax.text(x + width / 2, y + 0.0012, f"{km:g} km", ha="center", va="bottom", fontsize=8,
                bbox=dict(facecolor="white", alpha=0.7, lw=0, pad=1))


def relocation_figure(run: Run) -> None:
    dem = run.read(run.dem_path)
    water = run.read(run.find("*_LAND_Raster.tif")) == 80
    source = run.streams("*_StrmShp.parquet")
    moved = run.streams("*_matched.parquet")

    fig, axes = plt.subplots(2, 1, figsize=(8, 6.9))
    for ax, title in zip(axes, ("(a) The stream network as drawn", "(b) After relocating and moving")):
        run.base_map(ax, dem, water)
        ax.set_title(title, loc="left")
        run.scale_bar(ax)
    source.plot(ax=axes[0], color=SOURCE, lw=1.4)
    source.plot(ax=axes[1], color=SOURCE, lw=1.0, linestyle="--", alpha=0.8)
    moved.plot(ax=axes[1], color=MOVED, lw=1.5)
    water_patch = Patch(facecolor=WATER, label="Water (land cover)")
    axes[0].legend(handles=[Line2D([], [], color=SOURCE, lw=1.4, label="GEOGLOWS streams"), water_patch],
                   loc="upper right", framealpha=0.9)
    axes[1].legend(handles=[Line2D([], [], color=SOURCE, lw=1.0, ls="--", label="GEOGLOWS streams"),
                            Line2D([], [], color=MOVED, lw=1.5, label="Moved streams"), water_patch],
                   loc="upper right", framealpha=0.9)
    fig.text(0.01, 0.0, CREDIT, fontsize=7, color="0.4")
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    save(fig, "stream_relocation.png")


def _wse(run: Run, bed: np.ndarray, return_period: int) -> np.ndarray:
    depth = run.read(run.find(f"*_ARC_FloodDepth_rp{return_period}.tif"))
    return np.where(depth > 0, bed + depth, np.nan)


def flood_map_figure(run: Run) -> None:
    dem = run.read(run.dem_path)
    water = run.read(run.find("*_LAND_Raster.tif")) == 80
    burned = run.read(run.find("*_fixed.tif"))
    bed = run.read(run.find("*_FS_Bathy.tif"))
    depth100 = run.read(run.find("*_ARC_FloodDepth_rp100.tif"))
    depth2 = run.read(run.find("*_ARC_FloodDepth_rp2.tif"))
    moved = run.streams("*_matched.parquet")

    fig, ax = plt.subplots(figsize=(8, 3.9))
    run.base_map(ax, dem, np.zeros_like(water))
    flooded = np.ma.masked_where(~(depth100 > 0), depth100)
    image = ax.imshow(flooded, cmap="Blues", vmin=0, vmax=8, extent=run.extent, interpolation="nearest", alpha=0.9)
    ax.contour(np.where(depth2 > 0, 1.0, 0.0), levels=[0.5], colors=["#cb181d"], linewidths=0.8,
               extent=run.extent, origin="upper")
    moved.plot(ax=ax, color="k", lw=0.6)
    ax.plot([SECTION_X, SECTION_X], SECTION_Y, color="k", lw=1.2)
    for y, label, va in ((SECTION_Y[0], "A", "top"), (SECTION_Y[1], "A'", "bottom")):
        ax.text(SECTION_X, y, label, ha="center", va=va, fontsize=9, fontweight="bold",
                bbox=dict(facecolor="white", alpha=0.8, lw=0, pad=1))
    run.scale_bar(ax)
    bar = fig.colorbar(image, ax=ax, fraction=0.025, pad=0.01)
    bar.set_label("100-year flood depth (m)")
    ax.legend(handles=[Line2D([], [], color="#cb181d", lw=0.8, label="2-year flood extent"),
                       Line2D([], [], color="k", lw=0.6, label="Moved streams")],
              loc="upper right", framealpha=0.9)
    fig.text(0.01, 0.0, "Elevation: FABDEM. Flows: GEOGLOWS return periods.", fontsize=7, color="0.4")
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    save(fig, "flood_map.png")

    # ARC ran on the burned DEM, which the bathymetry is added to
    section_figure(run, burned, bed, _wse(run, bed, 2), _wse(run, bed, 100), water)


def section_figure(run: Run, dem, bed, wse2, wse100, water) -> None:
    col = int(round((SECTION_X - run.extent[0]) / run.gt[1] - 0.5))
    rows = np.arange(int((run.extent[3] - SECTION_Y[1]) / -run.gt[5]),
                     int((run.extent[3] - SECTION_Y[0]) / -run.gt[5]) + 1)[::-1]  # south to north
    dist = (np.arange(rows.size)) * run.dy_m
    ground, surface = bed[rows, col], dem[rows, col]
    low = np.nanmin(ground) - 2

    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.fill_between(dist, low, ground, step="mid", color="#d9c8a9", lw=0)
    ax.fill_between(dist, ground, wse100[rows, col], step="mid", color="#c6dbef", lw=0, label="100-year flood")
    ax.fill_between(dist, ground, wse2[rows, col], step="mid", color="#4292c6", lw=0, label="2-year flood")
    ax.step(dist, surface, where="mid", color="0.45", lw=1.0, ls=(0, (3, 2)), label="DEM")
    ax.step(dist, ground, where="mid", color="k", lw=1.0, label="DEM with bathymetry")
    on_water = np.where(water[rows, col], low + 0.6, np.nan)
    ax.step(dist, on_water, where="mid", color=WATER, lw=5, label="Water (land cover)")
    ax.set_xlim(dist[0], dist[-1])
    ax.set_ylim(low, np.nanmax(wse100[rows, col]) + 3)
    ax.set_xlabel("Distance from A (m)")
    ax.set_ylabel("Elevation (m)")
    ax.text(0.005, 0.98, "A", transform=ax.transAxes, ha="left", va="top", fontweight="bold")
    ax.text(0.995, 0.98, "A'", transform=ax.transAxes, ha="right", va="top", fontweight="bold")
    ax.legend(loc="upper center", ncol=3, framealpha=0.9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    save(fig, "bathymetry_cross_section.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--run", type=Path, help="The N39W109 run's folder.")
    parser.add_argument("--dem", type=Path, help="The FABDEM tile N39W109_FABDEM_V1-2.tif.")
    args = parser.parse_args()

    stream_moving_figure()
    if args.run and args.dem:
        run = Run(args.run, args.dem)
        relocation_figure(run)
        flood_map_figure(run)
    else:
        print("Without --run and --dem, only the synthetic figure is made.")


if __name__ == "__main__":
    main()
