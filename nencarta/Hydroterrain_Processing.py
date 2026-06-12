# built-in imports
import os
import math
import logging

# third-party imports
from osgeo import gdal, osr, ogr
from whitebox import WhiteboxTools
import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import unary_union, linemerge
import numpy as np
import pandas as pd

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


def _add_area_sq_km_field(
    gdf: gpd.GeoDataFrame,
    field_name: str = "area_sq_km",
) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        raise ValueError("Catchments must have a CRS to compute area in square kilometers.")

    area_source = gdf
    linear_unit_name = ""
    if getattr(gdf.crs, "axis_info", None):
        linear_unit_name = (gdf.crs.axis_info[0].unit_name or "").lower()

    if gdf.crs.is_geographic or linear_unit_name not in {"metre", "meter", "m"}:
        projected_crs = gdf.estimate_utm_crs()
        if projected_crs is None:
            raise ValueError("Could not determine a projected CRS for catchment area calculation.")
        area_source = gdf.to_crs(projected_crs)

    gdf = gdf.copy()
    gdf[field_name] = area_source.geometry.area.astype(float) / 1_000_000.0
    return gdf


def _normalize_stream_line(geom):
    # Reduce multiline artifacts to a single representative line so endpoint
    # tests and downstream-point sampling remain stable.
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "LineString":
        return geom
    if geom.geom_type == "MultiLineString":
        merged = linemerge(geom)
        if merged.geom_type == "LineString":
            return merged
        if merged.geom_type == "MultiLineString":
            parts = [part for part in merged.geoms if part is not None and not part.is_empty]
            if not parts:
                return None
            return max(parts, key=lambda g: g.length)
    return None


def _sample_point_near_downstream_end(geom, sample_back_distance: float):
    # Sample slightly upstream of the outlet so the point falls inside the
    # downstream catchment instead of exactly on a catchment boundary.
    line = _normalize_stream_line(geom)
    if line is None:
        return None
    if line.length <= 0:
        end = line.coords[-1]
        return Point(float(end[0]), float(end[1]))
    back_distance = min(max(float(sample_back_distance), 0.0), float(line.length))
    return line.interpolate(max(float(line.length) - back_distance, 0.0))


def _get_endpoints(geom, decimals=3):
    line = _normalize_stream_line(geom)
    if line is None:
        return None, None
    start = line.coords[0]
    end = line.coords[-1]
    return (round(start[0], decimals), round(start[1], decimals)), (
        round(end[0], decimals),
        round(end[1], decimals),
    )


def _build_vector_stream_topology(stream_parts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    # Infer vector topology from intact reach endpoints. This assumes lines are
    # already segmented only at real junctions, not at catchment boundaries.
    start_nodes = {}
    for _, row in stream_parts.iterrows():
        start_pt, _ = _get_endpoints(row.geometry)
        if start_pt:
            start_nodes[start_pt] = row["stream_id"]

    def find_downstream(geom):
        _, end_pt = _get_endpoints(geom)
        if end_pt:
            return start_nodes.get(end_pt, -1)
        return -1

    stream_parts = stream_parts.copy()
    stream_parts["downstream_id"] = stream_parts.geometry.apply(find_downstream).astype(np.int64)

    upstream_map = {}
    for _, row in stream_parts.iterrows():
        downstream_id = row["downstream_id"]
        if downstream_id != -1:
            upstream_map.setdefault(downstream_id, []).append(str(row["stream_id"]))

    stream_parts["upstream_ids"] = stream_parts["stream_id"].apply(
        lambda sid: ",".join(upstream_map.get(sid, []))
    )
    return stream_parts


def _assign_streams_to_catchments_by_downstream_point(
    streams_gdf: gpd.GeoDataFrame,
    catchments_gdf: gpd.GeoDataFrame,
    sample_back_distance: float,
) -> gpd.GeoDataFrame:
    """
    Assign each intact stream line to the catchment containing a point sampled
    just upstream of its downstream endpoint.

    This avoids clipping stream geometry at catchment boundaries while still
    carrying a single catchment identifier onto each reach.
    """
    sample_rows = []
    for idx, geom in streams_gdf.geometry.items():
        sample_point = _sample_point_near_downstream_end(geom, sample_back_distance)
        if sample_point is None or sample_point.is_empty:
            continue
        sample_rows.append({"_stream_index": idx, "geometry": sample_point})

    if not sample_rows:
        raise ValueError("No valid downstream sample points could be created for stream assignment.")

    sample_points_gdf = gpd.GeoDataFrame(sample_rows, geometry="geometry", crs=streams_gdf.crs)
    assigned = gpd.sjoin(
        sample_points_gdf,
        catchments_gdf[["catchment_id", "geometry"]],
        how="left",
        predicate="within",
    )[["_stream_index", "catchment_id"]]
    assigned = assigned.drop_duplicates(subset=["_stream_index"], keep="first")

    matched_streams = set(assigned.loc[assigned["catchment_id"].notna(), "_stream_index"].tolist())
    unmatched_mask = ~sample_points_gdf["_stream_index"].isin(matched_streams)
    if unmatched_mask.any():
        nearest = gpd.sjoin_nearest(
            sample_points_gdf.loc[unmatched_mask],
            catchments_gdf[["catchment_id", "geometry"]],
            how="left",
            distance_col="_catchment_dist",
        )[["_stream_index", "catchment_id"]]
        nearest = nearest.drop_duplicates(subset=["_stream_index"], keep="first")
        assigned = pd.concat([assigned, nearest], ignore_index=True)
        assigned = assigned.drop_duplicates(subset=["_stream_index"], keep="first")

    catchment_map = assigned.set_index("_stream_index")["catchment_id"].to_dict()
    stream_parts = streams_gdf.copy()
    stream_parts["catchment_id"] = stream_parts.index.map(catchment_map)
    stream_parts = stream_parts[stream_parts["catchment_id"].notna()].copy()
    if len(stream_parts) != len(streams_gdf):
        missing = len(streams_gdf) - len(stream_parts)
        raise ValueError(f"Could not assign catchment IDs to {missing} stream features.")

    stream_parts["catchment_id"] = pd.to_numeric(stream_parts["catchment_id"], errors="raise").astype(np.int64)
    stream_parts = stream_parts.reset_index(drop=True)
    return stream_parts

def create_catchments_and_flowlines_with_flow_direction_and_accumulation(
    flowdir_raster: str,
    flowacc_raster: str,
    out_dir: str,
    stream_threshold_km2: float = 5.0,
    out_flowlines_gpkg: str | None = None,
    out_catchments_gpkg: str | None = None,
    catchment_assignment_mode: str = "downstream_point",
):
    """
    Build threshold-based streams and catchments directly from flowdir/FAC rasters,
    and assess topology to determine upstream and downstream connectivity.

    Parameters
    ----------
    catchment_assignment_mode:
        ``"downstream_point"`` keeps each vectorized stream line intact and
        assigns its ``catchment_id`` from a point sampled just upstream of the
        line's downstream endpoint. This avoids the small stream slivers that
        can appear when stream lines are clipped at catchment boundaries.

        ``"intersection"`` preserves the legacy behavior that intersects
        vectorized stream lines with catchment polygons before topology is
        derived from endpoints.
    """
    os.makedirs(out_dir, exist_ok=True)
    valid_assignment_modes = {"downstream_point", "intersection"}
    if catchment_assignment_mode not in valid_assignment_modes:
        raise ValueError(
            f"Unsupported catchment_assignment_mode '{catchment_assignment_mode}'. "
            f"Expected one of: {sorted(valid_assignment_modes)}"
        )

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
    sample_back_distance = max(abs(float(fa_gt[1])), abs(float(fa_gt[5])), math.sqrt(cell_area) * 0.5)

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

    # Carry catchment IDs onto stream reaches without forcing geometry to be
    # split at every catchment boundary unless the legacy mode is requested.
    catchments_gdf = gpd.read_file(out_catchments_gpkg, layer=catchments_layer_name)
    if catchments_gdf.empty:
        raise ValueError("No catchments were generated from the thresholded stream mask.")
    if catchments_gdf.crs is None and fa_proj:
        catchments_gdf = catchments_gdf.set_crs(fa_proj)
    if streams_gdf.crs != catchments_gdf.crs:
        streams_gdf = streams_gdf.to_crs(catchments_gdf.crs)

    catchments_gdf = catchments_gdf[["catchment_id", "geometry"]].dissolve(by="catchment_id", as_index=False)
    catchments_gdf = _add_area_sq_km_field(catchments_gdf)
    _write_single_layer_gpkg(catchments_gdf, out_catchments_gpkg, catchments_layer_name)

    if catchment_assignment_mode == "downstream_point":
        stream_parts = _assign_streams_to_catchments_by_downstream_point(
            streams_gdf[["geometry"]].copy(),
            catchments_gdf,
            sample_back_distance=sample_back_distance,
        )
    else:
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
    stream_parts["stream_id"] = np.arange(1, len(stream_parts) + 1, dtype=np.int64)
    stream_parts["id"] = stream_parts["stream_id"]
    stream_parts = _build_vector_stream_topology(stream_parts)

    _write_single_layer_gpkg(stream_parts, out_flowlines_gpkg, flowlines_layer_name)

    return {
        "flowlines_vector": out_flowlines_gpkg,
        "catchments_vector": out_catchments_gpkg,
        "stream_threshold_cells": threshold_cells,
        "stream_threshold_km2": float(stream_threshold_km2),
    }


def match_new_streams_to_old_streams(
    new_streams_vector: str,
    old_streams_vector: str,
    out_streams_vector: str | None = None,
    old_linkno_field: str = "LINKNO",
    old_dslinkno_field: str = "DSLINKNO",
    old_stream_order_field: str | None = "StrmOrder",
    min_match_score: float = 0.05,
    require_overlap: bool = True,
    remove_detached_upstream: bool = True,
    connectivity_tolerance_m: float = 30.0,
    buffer_distance_m: float = 50.0,
):
    """
    Match new stream segments to an old reference stream network and transfer
    stream attributes stream ID (e.g., LINKNO or COMID) from the best match.

    Method summary
    - Reprojects both networks to a local metric CRS (UTM) so all distances and
      buffer sizes are interpreted in meters.
    - Builds a buffer around each new and old stream segment.
    - For each new stream segment, finds nearby old-stream buffers and ranks
      candidates by buffered overlap score, using overlap area as a tie-breaker.
    - Optionally requires geometric intersection (`require_overlap=True`).
    - Keeps only the highest-overlap candidate per new segment.
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
    - `old_stream_order_field`: Optional field in `old_streams_vector` holding
      stream order values to transfer into the matched output. When `None` or
      not present, stream order is skipped.
    - `min_match_score`: Minimum buffered-overlap score required to keep a match.
    - `require_overlap`: If `True`, candidate lines must intersect.
    - `remove_detached_upstream`: If `True`, remove detached subnetworks by
      retaining only the largest connected component.
    - `connectivity_tolerance_m`: Distance tolerance used to connect near-touching
      lines when `remove_detached_upstream=True`.
    - `buffer_distance_m`: Buffer distance, in meters, used to compare new and
      old stream corridor overlap.

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
    old_linkno_col = old_cols_upper[old_linkno_field.upper()]
    old_dslinkno_col = old_cols_upper[old_dslinkno_field.upper()]
    # Stream order transfer is optional. Only resolve it when explicitly
    # requested and present on the old-stream layer.
    if old_stream_order_field is not None and old_stream_order_field.upper() in old_cols_upper:
        old_stream_order_col = old_cols_upper[old_stream_order_field.upper()]
    else:
        old_stream_order_col = None

    # Align CRS first: convert old streams into the new-stream CRS.
    if old_streams.crs != new_streams.crs:
        old_streams = old_streams.to_crs(new_streams.crs)

    # Drop invalid geometries before building the matching index.
    old_streams = old_streams[old_streams.geometry.notnull() & (~old_streams.geometry.is_empty)].copy()
    new_streams = new_streams[new_streams.geometry.notnull() & (~new_streams.geometry.is_empty)].copy()
    if old_streams.empty or new_streams.empty:
        raise ValueError("No valid stream geometries available after filtering null/empty.")
    if buffer_distance_m <= 0:
        raise ValueError("buffer_distance_m must be > 0.")

    # Precompute old stream centroids and buffers for fast candidate lookup.
    old_streams["_oidx"] = np.arange(len(old_streams), dtype=np.int64)
    old_streams["_centroid"] = old_streams.geometry.centroid
    old_streams["_buffer_geom"] = old_streams.geometry.buffer(buffer_distance_m)
    old_buffer_sindex = gpd.GeoSeries(old_streams["_buffer_geom"], crs=old_streams.crs).sindex

    # Precompute new stream centroids for diagnostics and keep a stable source-row id
    # so matched siblings can be reattached after any optional topology filtering.
    new_streams["_src_oid"] = np.arange(len(new_streams), dtype=np.int64)
    new_streams["_oidx"] = np.arange(len(new_streams), dtype=np.int64)
    new_streams["_centroid"] = new_streams.geometry.centroid

    # Match each new stream to the old stream with the greatest buffered overlap.
    matched_rows = []
    for nidx, nrow in new_streams.iterrows():
        ngeom = nrow.geometry
        ncent = nrow["_centroid"]
        nbuf = ngeom.buffer(buffer_distance_m)
        nbuf_area = max(float(nbuf.area), 1e-9)

        # Candidate search by buffered stream corridor to reduce comparisons.
        cand_ids = list(old_buffer_sindex.intersection(nbuf.bounds))
        if not cand_ids:
            continue

        best = None

        for oid in cand_ids:
            orow = old_streams.iloc[oid]
            ogeom = orow.geometry
            if ogeom is None or ogeom.is_empty:
                continue
            obuf = orow["_buffer_geom"]

            cdist = float(ncent.distance(orow["_centroid"]))
            ldist = float(ngeom.distance(ogeom))

            # Optional strict overlap requirement.
            if require_overlap and (not ngeom.intersects(ogeom)):
                continue

            overlap_area = float(nbuf.intersection(obuf).area)
            if overlap_area <= 0.0:
                continue
            score = overlap_area / nbuf_area

            # Keep the candidate with the strongest buffered overlap, then break
            # ties by larger overlap area and smaller centroid distance.
            if (
                (best is None)
                or (score > best["score"])
                or (
                    math.isclose(score, best["score"])
                    and (
                        (overlap_area > best["overlap_area_m2"])
                        or (
                            math.isclose(overlap_area, best["overlap_area_m2"])
                            and (cdist < best["centroid_dist_m"])
                        )
                    )
                )
            ):
                best = {
                    "score": score,
                    "LINKNO": orow[old_linkno_col],
                    "DSLINKNO": orow[old_dslinkno_col],
                    "stream_order": (orow[old_stream_order_col] if old_stream_order_col is not None else None),
                    "centroid_dist_m": cdist,
                    "line_dist_m": ldist,
                    "overlap_area_m2": overlap_area,
                    "overlap_ratio": score,
                    "overlap_hit": int(ngeom.intersects(ogeom)),
                }

        # Reject unmatched or low-confidence matches.
        if best is None:
            continue
        if best["score"] < min_match_score:
            continue

        # Copy new stream row and append transferred attributes + diagnostics.
        out = dict(nrow.drop(labels=["_oidx", "_centroid"], errors="ignore"))
        out["LINKNO"] = best["LINKNO"]
        out["DSLINKNO"] = best["DSLINKNO"]
        if old_stream_order_col is not None:
            out[old_stream_order_col] = best["stream_order"]
        out["match_score"] = float(best["score"])
        out["centroid_dist_m"] = float(best["centroid_dist_m"])
        out["line_dist_m"] = float(best["line_dist_m"])
        out["overlap_area_m2"] = float(best["overlap_area_m2"])
        out["overlap_ratio"] = float(best["overlap_ratio"])
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

    # Once direct matches are finalized, include any additional new-stream
    # features sharing a matched stream_id and copy the same transferred old-
    # stream attributes and diagnostics onto those sibling features.
    if "stream_id" in matched_out.columns and "stream_id" in new_streams.columns and "_src_oid" in matched_out.columns:
        matched_stream_ids = matched_out["stream_id"].dropna().unique().tolist()
        if matched_stream_ids:
            existing_src_oids = set(pd.to_numeric(matched_out["_src_oid"], errors="coerce").dropna().astype(np.int64).tolist())
            sibling_candidates = new_streams[new_streams["stream_id"].isin(matched_stream_ids)].copy()
            sibling_candidates = sibling_candidates[
                ~pd.to_numeric(sibling_candidates["_src_oid"], errors="coerce").fillna(-1).astype(np.int64).isin(existing_src_oids)
            ].copy()

            if not sibling_candidates.empty:
                transfer_cols = [
                    col
                    for col in [
                        "LINKNO",
                        "DSLINKNO",
                        old_stream_order_col,
                        "match_score",
                        "centroid_dist_m",
                        "line_dist_m",
                        "overlap_area_m2",
                        "overlap_ratio",
                        "overlap_hit",
                    ]
                    if col is not None and col in matched_out.columns
                ]
                template_cols = ["stream_id"] + transfer_cols
                stream_templates = matched_out[template_cols].drop_duplicates(subset=["stream_id"], keep="first")
                sibling_out = sibling_candidates.merge(stream_templates, on="stream_id", how="left")
                sibling_out = sibling_out[matched_out.columns]
                matched_out = gpd.GeoDataFrame(
                    pd.concat([matched_out, sibling_out], ignore_index=True),
                    geometry="geometry",
                    crs=matched_out.crs,
                )

    matched_out = matched_out.drop(columns=["_oidx", "_centroid", "_src_oid"], errors="ignore")

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
