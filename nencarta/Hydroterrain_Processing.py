# built-in imports
import os
import math
import logging

# third-party imports
from osgeo import gdal, osr, ogr
from whitebox import WhiteboxTools
import geopandas as gpd
from shapely.ops import unary_union, linemerge
import numpy as np

from . import LOG

def create_flow_direction_and_flow_accumulation_raster(dem: str, filled_dem: str, out_dir: str, flowdir: str, flowacc: str = None):
    wbt = WhiteboxTools()
    wbt.set_verbose_mode(LOG.level <= logging.INFO)
    wbt.set_compress_rasters(True)
    wbt.work_dir = out_dir

    # remove all the old files before proceeding
    if os.path.exists(filled_dem):
        os.remove(filled_dem)
    if os.path.exists(flowdir):
        os.remove(flowdir)
    if os.path.exists(flowacc):
        os.remove(flowacc)

    # Whitebox operations
    wbt.fill_depressions_wang_and_liu(dem = dem, output = filled_dem, fix_flats=True) # Optionally remove flat areas
    wbt.d8_pointer(dem = filled_dem, output = flowdir)
    # Build flow accumulation from the same hydrologically conditioned DEM
    # used for D8 flow direction so both rasters are consistent.
    wbt.d8_flow_accumulation(
                                filled_dem,
                                flowacc,
                                out_type="specific contributing area", # Output type: 'cells' (default), 'catchment area', or 'specific contributing area'
                                log=False,
                                clip=False
                            )
        
def _write_single_layer_gpkg(gdf: gpd.GeoDataFrame, gpkg_path: str, layer_name: str):
    # Recreate the GeoPackage so it contains exactly one layer.
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(gpkg_path):
        drv.DeleteDataSource(gpkg_path)
    gdf.to_file(gpkg_path, layer=layer_name, driver="GPKG")

def create_catchments_and_flowlines_with_flow_direction_and_accumulation(
    flowdir_raster: str,
    flowacc_raster: str,
    out_dir: str,
    stream_threshold_km2: float = 5.0,
    out_flowlines_gpkg: str | None = None,
    out_catchments_gpkg: str | None = None,
):
    """
    Build threshold-based streams and catchments directly from flowdir/FAC rasters.
    """
    os.makedirs(out_dir, exist_ok=True)

    if out_flowlines_gpkg is None:
        out_flowlines_gpkg = os.path.join(out_dir, "flowline_network_thresholded.gpkg")
    if out_catchments_gpkg is None:
        out_catchments_gpkg = os.path.join(out_dir, "catchments_for_flowline_network.gpkg")
    catchments_layer_name = "catchments"
    flowlines_layer_name = "flowlines"

    wbt = WhiteboxTools()
    wbt.set_compress_rasters(True)
    wbt.work_dir = out_dir

    stream_mask = os.path.join(out_dir, "stream_mask_thresholded.tif")
    stream_lines_shp = os.path.join(out_dir, "stream_lines_thresholded.shp")
    subbasins_raster = os.path.join(out_dir, "catchments_thresholded.tif")

    # remove all the old files before proceeding
    if os.path.exists(out_flowlines_gpkg):
        os.remove(out_flowlines_gpkg)
    if os.path.exists(out_catchments_gpkg):
        os.remove(out_catchments_gpkg)
    if os.path.exists(stream_mask):
        os.remove(stream_mask)
    if os.path.exists(stream_lines_shp):
        os.remove(stream_lines_shp)
    if os.path.exists(subbasins_raster):
        os.remove(subbasins_raster)

    # Convert threshold area to flow-accumulation cells using raster pixel area.
    fa_ds = gdal.Open(flowacc_raster, gdal.GA_ReadOnly)
    if fa_ds is None:
        raise FileNotFoundError(f"Could not open flow accumulation raster: {flowacc_raster}")
    fa_gt = fa_ds.GetGeoTransform()
    fa_proj = fa_ds.GetProjection()
    cell_area = abs(float(fa_gt[1]) * float(fa_gt[5]))
    fa_ds = None
    if cell_area <= 0:
        raise ValueError("Invalid flow accumulation raster pixel area.")
    threshold_cells = max(1, int(round((stream_threshold_km2) * 1000000)))

    # Extract and vectorize thresholded stream network from FAC + D8 pointer.
    wbt.extract_streams(flow_accum=flowacc_raster, output=stream_mask, threshold=threshold_cells)
    wbt.raster_streams_to_vector(stream_mask, flowdir_raster, stream_lines_shp)
    wbt.subbasins(d8_pntr=flowdir_raster, streams=stream_mask, output=subbasins_raster)

    streams_gdf = gpd.read_file(stream_lines_shp)
    if streams_gdf.empty:
        raise ValueError("No stream features were extracted for the specified threshold.")
    if streams_gdf.crs is None:
        if not fa_proj:
            raise ValueError("Extracted stream lines have no CRS and flow rasters have no projection.")
        streams_gdf = streams_gdf.set_crs(fa_proj)

    # Polygonize threshold-based subbasin raster into catchment polygons.
    sub_ds = gdal.Open(subbasins_raster, gdal.GA_ReadOnly)
    if sub_ds is None:
        raise FileNotFoundError(f"Could not open generated subbasin raster: {subbasins_raster}")
    sub_band = sub_ds.GetRasterBand(1)
    sub_nodata = sub_band.GetNoDataValue()

    gpkg_driver = ogr.GetDriverByName("GPKG")
    if os.path.exists(out_catchments_gpkg):
        gpkg_driver.DeleteDataSource(out_catchments_gpkg)
    out_ds = gpkg_driver.CreateDataSource(out_catchments_gpkg)
    if out_ds is None:
        raise RuntimeError(f"Could not create catchment vector: {out_catchments_gpkg}")

    out_srs = osr.SpatialReference()
    if fa_proj:
        out_srs.ImportFromWkt(fa_proj)
    layer = out_ds.CreateLayer(catchments_layer_name, srs=out_srs, geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("catchment_id", ogr.OFTInteger))
    catchment_field_idx = layer.GetLayerDefn().GetFieldIndex("catchment_id")
    gdal.Polygonize(sub_band, None, layer, catchment_field_idx, [], callback=None)

    # Remove background/no-data polygons.
    to_delete = []
    layer.ResetReading()
    for feat in layer:
        v = feat.GetField("catchment_id")
        if v is None:
            to_delete.append(feat.GetFID())
            continue
        if sub_nodata is not None and float(v) == float(sub_nodata):
            to_delete.append(feat.GetFID())
            continue
        if int(v) <= 0:
            to_delete.append(feat.GetFID())
    for fid in to_delete:
        layer.DeleteFeature(fid)

    layer = None
    out_ds = None
    sub_ds = None

    # Ensure streams and catchments have matching IDs by intersecting streams
    # with delineated catchments and carrying catchment_id onto each stream part.
    catchments_gdf = gpd.read_file(out_catchments_gpkg, layer=catchments_layer_name)
    if catchments_gdf.empty:
        raise ValueError("No catchments were generated from the thresholded stream mask.")
    if catchments_gdf.crs is None and fa_proj:
        catchments_gdf = catchments_gdf.set_crs(fa_proj)
    if streams_gdf.crs != catchments_gdf.crs:
        streams_gdf = streams_gdf.to_crs(catchments_gdf.crs)

    catchments_gdf = catchments_gdf[["catchment_id", "geometry"]].dissolve(by="catchment_id", as_index=False)
    _write_single_layer_gpkg(catchments_gdf, out_catchments_gpkg, catchments_layer_name)

    stream_parts = gpd.overlay(
        streams_gdf[["geometry"]],
        catchments_gdf[["catchment_id", "geometry"]],
        how="intersection",
        keep_geom_type=True,
    )
    stream_parts = stream_parts[~stream_parts.geometry.is_empty]
    stream_parts = stream_parts[stream_parts.geometry.notnull()]
    stream_parts = stream_parts[stream_parts.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    if stream_parts.empty:
        raise ValueError("No stream segments overlap generated catchments.")

    stream_parts["catchment_id"] = stream_parts["catchment_id"].astype(np.int64)
    stream_parts["stream_id"] = stream_parts["catchment_id"]
    stream_parts["id"] = stream_parts["catchment_id"]
    _write_single_layer_gpkg(stream_parts, out_flowlines_gpkg, flowlines_layer_name)

    return {
        "flowlines_vector": out_flowlines_gpkg,
        "catchments_vector": out_catchments_gpkg,
        "stream_threshold_cells": threshold_cells,
        "stream_threshold_km2": float(stream_threshold_km2),
    }

# Estimate segment bearing from first-to-last vertex.
def _bearing_deg(line):
    if line is None or line.is_empty:
        return None
    if line.geom_type == "MultiLineString":
        line = linemerge(line)
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
    coords = list(line.coords)
    if len(coords) < 2:
        return None
    x0, y0 = coords[0]
    x1, y1 = coords[-1]
    if (x1 == x0) and (y1 == y0):
        return None
    return math.degrees(math.atan2(y1 - y0, x1 - x0))

# Convert angular difference to [0,1] similarity (1 is best).
# Reversed line direction is treated as equivalent.
def _angle_similarity(a1, a2):
    if a1 is None or a2 is None:
        return 0.5
    d = abs(a1 - a2) % 360.0
    d = min(d, 360.0 - d)
    d = min(d, abs(180.0 - d))  # treat reversed direction as equivalent
    return max(0.0, 1.0 - (d / 90.0))

def match_new_streams_to_old_streams(
    new_streams_vector: str,
    old_streams_vector: str,
    out_streams_vector: str | None = None,
    old_linkno_field: str = "LINKNO",
    old_dslinkno_field: str = "DSLINKNO",
    old_stream_order_field: str = "StrmOrder",
    max_centroid_distance_m: float = 300.0,
    min_match_score: float = 0.55,
    require_overlap: bool = True,
    remove_detached_upstream: bool = True,
    connectivity_tolerance_m: float = 30.0,
):
    """
    Match new stream segments to an old reference stream network and transfer
    stream attributes stream ID (e.g., LINKNO or COMID) from the best match.

    Method summary
    - Reprojects both networks to a metric CRS (UTM if source CRS is geographic).
    - For each new stream segment, builds candidate old segments from a centroid
      distance search window.
    - Applies a hard centroid-distance gate.
    - Optionally requires geometric intersection (`require_overlap=True`).
    - Keeps only the closest centroid candidate per new segment.
    - Drops matches below `min_match_score`.
    - Optionally removes detached subnetworks from the matched output by keeping
      only the main connected component (largest total segment length), where
      connectivity is defined as `intersects` or within `connectivity_tolerance_m`.

    Parameters
    - `new_streams_vector`: Path to the new stream network (GPKG/other vector).
    - `old_streams_vector`: Path to the old/reference stream network containing
      fields used for transfer.
    - `out_streams_vector`: Output path for filtered/matched new streams. If
      `None`, overwrites `new_streams_vector`.
    - `old_linkno_field`: Field in `old_streams_vector` holding LINKNO values.
    - `old_dslinkno_field`: Field in `old_streams_vector` holding downstream
      LINKNO values.
    - `old_stream_order_field`: Field in `old_streams_vector` holding stream order.
    - `max_centroid_distance_m`: Max centroid-to-centroid distance allowed.
    - `max_line_distance_m`: Deprecated in centroid-only matching (ignored).
    - `min_match_score`: Minimum centroid-based score required to keep a match.
    - `require_overlap`: If `True`, candidate lines must intersect.
    - `remove_detached_upstream`: If `True`, remove detached subnetworks by
      retaining only the largest connected component.
    - `connectivity_tolerance_m`: Distance tolerance used to connect near-touching
      lines when `remove_detached_upstream=True`.

    Returns
    - Dict with output path and basic counts:
      `stream_network_vector`, `matched_count`, `input_count`, `dropped_count`.

    Raises
    - `ValueError` for missing/empty/invalid geometry inputs or when no matches
      survive thresholds.
    - `KeyError` when required old-stream fields are missing.
    """
    # Write/read the stream network from a single, known layer name.
    flowlines_layer_name = "flowlines"
    if out_streams_vector is None:
        out_streams_vector = new_streams_vector

    # Load new streams (prefer explicit layer) and old reference streams.
    new_streams = gpd.read_file(new_streams_vector)
    old_streams = gpd.read_file(old_streams_vector)

    # Validate required inputs.
    if new_streams.empty:
        raise ValueError("New stream network is empty.")
    if old_streams.empty:
        raise ValueError("Old stream network is empty.")
    if new_streams.crs is None or old_streams.crs is None:
        raise ValueError("Both stream layers must have a CRS.")

    # Resolve old attribute fields case-insensitively.
    old_cols_upper = {c.upper(): c for c in old_streams.columns}
    if old_linkno_field.upper() not in old_cols_upper:
        raise KeyError(f"Missing old stream ID field '{old_linkno_field}'.")
    if old_dslinkno_field.upper() not in old_cols_upper:
        raise KeyError(f"Missing old stream downstream ID field '{old_dslinkno_field}'.")
    if old_stream_order_field.upper() not in old_cols_upper:
        raise KeyError(f"Missing old stream stream order field '{old_stream_order_field}'.")
    old_linkno_col = old_cols_upper[old_linkno_field.upper()]
    old_dslinkno_col = old_cols_upper[old_dslinkno_field.upper()]
    old_stream_order_col = old_cols_upper[old_stream_order_field.upper()]

    # Align CRS first: convert old streams into the new-stream CRS.
    if old_streams.crs != new_streams.crs:
        old_streams = old_streams.to_crs(new_streams.crs)

    # Drop invalid geometries before building the matching index.
    old_streams = old_streams[old_streams.geometry.notnull() & (~old_streams.geometry.is_empty)].copy()
    new_streams = new_streams[new_streams.geometry.notnull() & (~new_streams.geometry.is_empty)].copy()
    if old_streams.empty or new_streams.empty:
        raise ValueError("No valid stream geometries available after filtering null/empty.")

    # Precompute old stream centroids and spatial index for fast candidate lookup.
    old_streams["_oidx"] = np.arange(len(old_streams), dtype=np.int64)
    old_streams["_centroid"] = old_streams.geometry.centroid
    old_sindex = old_streams.sindex

    # Precompute new stream centroids and spatial index for fast candidate lookup.
    new_streams["_oidx"] = np.arange(len(new_streams), dtype=np.int64)
    new_streams["_centroid"] = new_streams.geometry.centroid
    # new_sindex = new_streams.sindex

    # Match each new stream to the nearest old-stream centroid.
    matched_rows = []
    for nidx, nrow in new_streams.iterrows():
        ngeom = nrow.geometry
        ncent = nrow["_centroid"]
        # Candidate search by centroid buffer to reduce comparisons.
        nb = ncent.buffer(max_centroid_distance_m).bounds
        cand_ids = list(old_sindex.intersection(nb))
        if not cand_ids:
            continue

        best = None

        for oid in cand_ids:
            orow = old_streams.iloc[oid]
            ogeom = orow.geometry
            if ogeom is None or ogeom.is_empty:
                continue

            # Hard distance gates: skip unlikely candidates early.
            cdist = float(ncent.distance(orow["_centroid"]))
            if cdist > max_centroid_distance_m:
                continue

            # Optional strict overlap requirement.
            if require_overlap and (not ngeom.intersects(ogeom)):
                continue

            # Centroid-only score.
            score = max(0.0, 1.0 - (cdist / max_centroid_distance_m))
            ldist = float(ngeom.distance(ogeom))

            # Keep only the closest-centroid old stream for this new stream.
            if (best is None) or (cdist < best["centroid_dist_m"]):
                best = {
                    "score": score,
                    "LINKNO": orow[old_linkno_col],
                    "DSLINKNO": orow[old_dslinkno_col],
                    "StrmOrder": orow[old_stream_order_col],
                    "centroid_dist_m": cdist,
                    "line_dist_m": ldist,
                    "overlap_hit": int(ngeom.intersects(ogeom)),
                }

        # Reject unmatched or low-confidence matches.
        if best is None:
            continue
        if best["score"] < min_match_score:
            continue

        # Copy new stream row and append transferred attributes + diagnostics.
        out = dict(nrow.drop(labels=["_centroid"], errors="ignore"))
        out["LINKNO"] = best["LINKNO"]
        out["DSLINKNO"] = best["DSLINKNO"]
        out["StrmOrder"] = best["StrmOrder"]
        out["match_score"] = float(best["score"])
        out["centroid_dist_m"] = float(best["centroid_dist_m"])
        out["line_dist_m"] = float(best["line_dist_m"])
        out["overlap_hit"] = int(best["overlap_hit"])
        matched_rows.append(out)

    if not matched_rows:
        raise ValueError("No new streams met the matching thresholds.")

    # Build matched GeoDataFrame in projected CRS for optional topology filtering.
    matched_proj = gpd.GeoDataFrame(matched_rows, geometry="geometry", crs=new_streams.crs).reset_index(drop=True)

    # Optionally remove detached subnetworks by keeping only the main connected
    # component (largest summed length). Connectivity is based on intersects()
    # or near-touching within connectivity_tolerance_m.
    if remove_detached_upstream:
        if connectivity_tolerance_m < 0:
            raise ValueError("connectivity_tolerance_m must be >= 0.")
        if len(matched_proj) > 0:
            geoms = list(matched_proj.geometry.values)
            lengths = [float(g.length) if g is not None and (not g.is_empty) else 0.0 for g in geoms]
            sindex = matched_proj.sindex
            adj = [set() for _ in range(len(geoms))]

            # Build undirected adjacency graph between connected/nearby segments.
            for i, gi in enumerate(geoms):
                if gi is None or gi.is_empty:
                    continue
                minx, miny, maxx, maxy = gi.bounds
                candidates = sindex.intersection(
                    (
                        minx - connectivity_tolerance_m,
                        miny - connectivity_tolerance_m,
                        maxx + connectivity_tolerance_m,
                        maxy + connectivity_tolerance_m,
                    )
                )
                for j in candidates:
                    if j <= i:
                        continue
                    gj = geoms[j]
                    if gj is None or gj.is_empty:
                        continue
                    if gi.intersects(gj) or (gi.distance(gj) <= connectivity_tolerance_m):
                        adj[i].add(j)
                        adj[j].add(i)

            # Find connected components with DFS.
            unvisited = set(range(len(geoms)))
            components = []
            while unvisited:
                seed = next(iter(unvisited))
                stack = [seed]
                comp = set()
                while stack:
                    n = stack.pop()
                    if n in comp:
                        continue
                    comp.add(n)
                    if n in unvisited:
                        unvisited.remove(n)
                    for nbr in adj[n]:
                        if nbr not in comp:
                            stack.append(nbr)
                components.append(comp)

            # Keep only the main component to remove detached upstream subnetworks.
            if components:
                main_comp = max(components, key=lambda c: sum(lengths[k] for k in c))
                matched_proj = matched_proj.iloc[sorted(main_comp)].copy()

    # Return to original new-stream CRS and write a single-layer GeoPackage.
    matched_out = matched_proj.to_crs(new_streams.crs) if matched_proj.crs != new_streams.crs else matched_proj

    # remove the old file before making the new one
    if os.path.exists(out_streams_vector):
        os.remove(out_streams_vector)

    _write_single_layer_gpkg(matched_out, out_streams_vector, flowlines_layer_name)

    return {
        "stream_network_vector": out_streams_vector,
        "matched_count": int(len(matched_out)),
        "input_count": int(len(new_streams)),
        "dropped_count": int(len(new_streams) - len(matched_out)),
    }
