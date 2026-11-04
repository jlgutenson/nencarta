# Configuration reference

Each watershed is described by a set of keys. The same keys are used by the
`watersheds` of a JSON file, the options of the `cli` command, the fields of
the GUI and the dictionary given to [`process_watershed`][nencarta.process_watershed]
(see [Usage](usage.md)).

Only these are required:

* `name` and `output_dir`;
* a DEM: `dem`, `dem_dir`, or `source_dems` with `bbox`;
* streams: `flowline` or `source_flowlines`.

Every other key has a default. A key NenCarta doesn't know, such as a misspelt
one, is ignored with a warning.

## Required

* <a id="json-name"></a>`name` (text): Name of the watershed. Its outputs go in
    `<output_dir>/<name>`.

* <a id="json-output_dir"></a>`output_dir` (path): Folder to write the outputs in.

## DEMs

NenCarta uses every DEM cell with data, including cells at 0 m: it doesn't
take them for the sea. Put the sea in `ocean` when burning streams, or give
it the DEM's nodata value, and other big water the streams flow into in
`lakes`.

* <a id="json-dem"></a>`dem` (path): A DEM file to map. Ignored when `dem_dir` is given.

* <a id="json-dem_dir"></a>`dem_dir` (path): A folder of DEMs. Each DEM matching `dem_filter` is
    mapped on its own, with its own outputs.

* <a id="json-dem_filter"></a>`dem_filter` (text, default `*`): File pattern that picks the DEMs in
    `dem_dir`, e.g. `*.tif`.

* <a id="json-source_dems"></a>`source_dems` (list of paths): DEMs to cut the domain's DEM from, for
    `bbox` or `buffer`. Use DEMs in geographic coordinates (degrees).

* <a id="json-bbox"></a>`bbox` (list of 4 numbers): Area to map, `[minx, miny, maxx, maxy]` in
    degrees. Its DEM is cut from `source_dems` (and `dem`, if given) at their
    resolution, and its outputs are named `dem_<minx>_<miny>_<maxx>_<maxy>`.

* <a id="json-buffer"></a>`buffer` (true/false, default false): Map an area `buffer_distance`
    larger than the DEM, cut from `source_dems`, so rivers flow in from outside
    it, then crop the flood maps back to the DEM. The other outputs are named
    `<DEM>_buffered`.

* <a id="json-buffer_distance"></a>`buffer_distance` (number, default 0.1): How far `buffer` grows the DEM,
    in degrees.

* <a id="json-use_vrt"></a>`use_vrt` (true/false, default false): Write the cut DEM as a VRT instead
    of a GeoTIFF.

* <a id="json-project_to_utm"></a>`project_to_utm` (true/false, default false): Project the cut DEM to its
    UTM zone. A DEM used as it is isn't projected.

* <a id="json-use_warning_flags_to_download_dem"></a>`use_warning_flags_to_download_dem` (true/false, default false): Download
    USGS 1/3 arc-second DEMs into `dem_dir` around the rivers that the GEOGLOWS
    forecast of `forensic_forecast_date` floods in `geoglows_vpu`, and map
    them. CONUS only. Needs `dem_dir`, `geoglows_vpu` and
    `forensic_forecast_date`.

* <a id="json-geoglows_vpu"></a>`geoglows_vpu` (text): The GEOGLOWS VPU (e.g. `714`) for
    `use_warning_flags_to_download_dem`.

## Streams

* <a id="json-flowline"></a>`flowline` (path): The stream network: a shapefile, GeoPackage, GeoParquet
    or other vector file of lines, one per reach, with the reach's ID and the ID
    of the reach downstream.

* <a id="json-source_flowlines"></a>`source_flowlines` (list of paths): Stream network files covering a larger
    area. The reaches overlapping each DEM are taken from them. Used when
    `flowline` is not given.

* <a id="json-stream_id_field"></a>`stream_id_field` (text): Attribute with each reach's ID, which flows are
    matched to. Default `LINKNO` for GEOGLOWS, `COMID` for NWM; required for
    a `CUSTOM` source.

* <a id="json-downstream_id_field"></a>`downstream_id_field` (text): Attribute with the ID of the reach
    downstream. Default `DSLINKNO` for GEOGLOWS, `TOCOMID` for NWM; required
    for a `CUSTOM` source.

* <a id="json-StrmOrder_Field"></a>`StrmOrder_Field` (text): Attribute with each reach's stream order. Used by
    `StrmOrder_Lower` and `StrmOrder_Upper`, and to favour bigger rivers when
    burning or moving streams.

* <a id="json-StrmOrder_Lower"></a>`StrmOrder_Lower` (integer): Map only reaches of at least this stream
    order. Not used by the `Curve2Flood-FLDPLNpy` mapper.

* <a id="json-StrmOrder_Upper"></a>`StrmOrder_Upper` (integer): Map only reaches of at most this stream order.
    Not used by the `Curve2Flood-FLDPLNpy` mapper.

* <a id="json-exclude"></a>`exclude` (list of integers): IDs of reaches to leave out.

* <a id="json-drop_multilinestrings"></a>`drop_multilinestrings` (true/false, default false): Leave out reaches
    whose line is in pieces that can't be joined into one line. When the streams
    are moved (`move_stream_network_to_thalweg`), they are left out before
    anything is relocated, burned or moved, so none of their lines is carved
    into the DEM. Kept (false), such a reach is moved along its trunk and longer
    branch, and only its trunk is burned. The trunk is the piece that joins the
    reach below; GEOGLOWS draws its headwater reaches as a trunk with two
    first-order branches forking from its top. Burned, the shorter branch would
    stay carved in the DEM, unmapped, and flood.

* <a id="json-lake_filter_json"></a>`lake_filter_json` (path): A GEOGLOWS JSON file of the reaches inside each
    lake, `{"<lake>": {"inside": [<reach ID>, ...]}}`. Those reaches are left
    out.

* <a id="json-lakes"></a>`lakes` (path): Lake polygons. Put the sea in `ocean`. When burning
    streams, the burned channel is not smoothed inside lakes. When moving streams with the
    `Curve2Flood-FLDPLNpy` mapper, lakes cut the network: no stream runs inside
    one, a reach flowing out of one starts in the river below its outlet rather
    than on the dam or shore between the two, and the FLDPLN library leaves lakes
    out. With the other mappers, a moved
    reach touching a lake is dropped, unless it carries a river through it. The
    land cover's water inside a lake is in the water mask whatever its level, when
    it touches the streams.

* <a id="json-ocean"></a>`ocean` (path): Ocean polygons. With `burn_streams`, the burned DEM
    (`fixed`) has no data in them, so the streams end at the shore and drain
    off the DEM there, as over its edge, and the water mask leaves the sea out.
    A river ends where it first reaches the sea: the reaches starting in the
    sea, and those below them, are left out, and a line running on out of the
    sea, along the shore or back onto the land, is cut there. A sea's polygon
    often reaches up an estuary, and leaves such a reach only the bank beside
    the channel, which it would flood as deep as the river. Unlike a sea in
    `lakes`, the ocean keeps each
    river's last reach on land with every mapper. Without
    `burn_streams`, `ocean` is ignored. A DEM's cells at 0 m aren't taken
    for the sea (see [DEMs](#dems)).

    Only the polygons overlapping the DEM are read, but a sea's polygon is read
    whole, and a global dataset's run to hundreds of thousands of vertices.
    Split them into tiles, say 1 degree, and save them as a GeoParquet with a
    bbox column (geopandas' `to_parquet(..., write_covering_bbox=True)`), with
    nearby tiles together in small row groups: each DEM then reads just the
    pieces around it, in a fraction of a second.

* <a id="json-move_stream_network_to_thalweg"></a>`move_stream_network_to_thalweg` (true/false, default false): Move each
    reach onto the DEM's flow path (see [Moving streams onto the DEM](methods.md#moving-streams-onto-the-dem)). Required
    by the `Curve2Flood-FLDPLNpy` mapper.

* <a id="json-new_strm_threshold_km2"></a>`new_strm_threshold_km2` (number, default 1): When moving streams, the
    smallest drainage area (km²) of the DEM flow paths reaches are moved onto.
    The part of a reach on a smaller flow path, usually the top of a headwater
    stream, is left out. A river flowing in from outside the DEM counts from where
    it enters.

* <a id="json-burn_streams"></a>`burn_streams` (true/false, default false): Burn the streams into the DEM
    (`<DEM>_fixed.tif`), which is used instead of the DEM from then on. Each
    reach is lowered along its whole line, in 0.5 m steps that never rise
    downstream, starting no higher than the largest reach flowing into it, and
    where reaches meet the larger one keeps the shared cell, so the burned rivers
    drain without breaks. A tributary whose mouth lies below the river's water is
    left in a pool there, as the river's backwater would leave it, rather than
    lowering the river. The reaches' sizes come from `StrmOrder_Field` and the
    drainage area; without them, a reach starts no higher than any reach flowing
    into it. The steps are then smoothed into
    ramps, and so is water the DEM shows in 0.5 m terraces, as FABDEM does (see
    [Moving streams onto the DEM](methods.md#moving-streams-onto-the-dem)). With
    `move_stream_network_to_thalweg`, the DEM is put back as it was wherever
    the burn changed it and no moved stream runs: more than a cell from every
    moved stream and from the water mask, and outside the lakes. A line burned
    where the moving doesn't follow it, such as the straight stretch stream
    relocation draws to keep a moved reach joined to the reach below it, would
    otherwise stay a trench no stream is mapped along, and flood. Burned cells
    the streams drain through stay: where putting them back would change the
    filled DEM at a stream or a lake, as a line burned through a reservoir's dam
    does, they are kept.

* <a id="json-relocate_misplaced_streams"></a>`relocate_misplaced_streams` (true/false, default true): With
    `move_stream_network_to_thalweg`, first move the reaches the unburned DEM
    shows are off their river, such as a river drawn straight across its
    floodplain, onto the flow path their lines drain along, so they are burned
    and moved there instead (see [Relocating misplaced streams](methods.md#relocating-misplaced-streams)). A reach moves only
    where the DEM shows a channel along that path that its line misses, and the
    path doesn't cut the meanders of a line on the floodplain. Needs the streams' drainage area
    (`area_km2_field` or `area_m2_field`). The relocated network is saved as
    `StrmShp_relocated` when any line changed.

* <a id="json-use_dem_derived_channel_mask"></a>`use_dem_derived_channel_mask` (true/false, default false): Find the
    channel the DEM shows along the streams, whatever the DEM (see
    [methods](methods.md#the-channel-a-dem-shows)). When burning streams, the water the DEM
    flattens is smoothed as water and the lines are left unlowered on it, and the
    whole channel joins the land cover's water in the bathymetry water mask.

* <a id="json-dem_flattens_rivers"></a>`dem_flattens_rivers` (`auto`, `yes` or `no`; or true/false, default `auto`):
    Whether the DEM flattens every river wide enough to show, for
    `use_dem_derived_channel_mask`'s channel (see
    [methods](methods.md#the-channel-a-dem-shows)). `yes` for FABDEM and
    Copernicus: a river they left unflattened is narrower than a cell, so the
    low ground beside it is floodplain and is left out of the water mask. `no`
    for USGS 3DEP, which flattens only some of its water: the low ground by the
    other rivers is their channel. `auto` says `yes` when at least 30% of the
    flat water by the streams is at whole half metres, as FABDEM and Copernicus
    flatten theirs. A run over many tiles of one DEM is best given its answer,
    so neighbouring tiles can't be told apart. It changes nothing on DEMs
    rounded to 0.1 m or coarser (GEDTM, SRTM, NASADEM, ASTER, AW3D30), where
    flatness says nothing and the low ground is always used.

* <a id="json-streams_as_parquet"></a>`streams_as_parquet` (true/false, default false): Write the stream networks
    as GeoParquet instead of GeoPackage.

* <a id="json-minimize_output_files"></a>`minimize_output_files` (true/false, default false): Delete the domain's
    source streams (`StrmShp`) and relocated streams (`StrmShp_relocated`) once
    the moved stream network is written.

## Flows

NenCarta builds a table of flow statistics for each reach (the "reanalysis"
file): return period flows (`rp2` to `rp100`), flow duration curve flows
(`p_exceed_0` to `p_exceed_100`), and two high flows for the rating curves,
`rp100_premium` and `p_exceed_0_premium` (1.5 times the flow, plus
50 m³/s). The flood maps are then made for forecast flows, return period flows
or your own flows.

* <a id="json-streamflow_source"></a>`streamflow_source` (text, default `GEOGLOWS_V2`): Where the flows come
    from: `GEOGLOWS_V2`, `NWM_short_range`, `NWM_medium_range`,
    `NWM_long_range` or `CUSTOM`. `GEOGLOWS` still means GEOGLOWS V2, and
    its files are still named `GEOGLOWS_...`. A `CUSTOM` source downloads
    nothing: it needs `stream_id_field` and `downstream_id_field`, and takes
    the flow statistics from `reanalysis_file`, or from `return_period_file`
    (with `fdc_file` for `p_exceed` flows). Its flood maps are made from
    `user_flow_files` or `return_periods`; there are no forecasts.

* <a id="json-nwm_api_key"></a>`nwm_api_key` (text): Key for the NWM API, required for the NWM sources. It
    can also be set in the `NWM_API_KEY` environment variable. To get one, see
    [these instructions](https://docs.ciroh.org/docuhub-staging/docs/products/data-management/bigquery-api/).

* <a id="json-floodmap_mode"></a>`floodmap_mode` (text, default `forecast`): Which flows to map:
    `forecast` (a forecast), `user` (`user_flow_files`) or
    `return_period` (`return_periods`).

* <a id="json-user_flow_files"></a>`user_flow_files` (path or list of paths): Flow files to map in `user`
    mode: CSV files with the reach ID in the first column and flows (m³/s) in the
    others. Each gets its own flood maps.

* <a id="json-return_periods"></a>`return_periods` (list of integers): Return periods to map in
    `return_period` mode: any of 2, 5, 10, 25, 50 and 100. GEOGLOWS or
    `CUSTOM` only.

* <a id="json-reanalysis_file"></a>`reanalysis_file` (path): A flow statistics file to use instead of building
    one: a CSV with a `COMID` column of reach IDs, the columns named by
    `specified_bathyflow_field` and `specified_highflow_field`, and, for
    `return_period` mode, `rp<N>` columns.

* <a id="json-return_period_file"></a>`return_period_file` (text): Return period flows to use instead of
    GEOGLOWS': a local path or URL (`s3://`, `https://`) to a CSV, Parquet,
    Zarr or NetCDF file. Zarr and NetCDF files need `river_id` and
    `return_period` dimensions; CSV and Parquet files need a `river_id` (or
    `stream_id_field`) column and `rp<N>` columns. GEOGLOWS or `CUSTOM`
    only.

* <a id="json-return_period_variables"></a>`return_period_variables` (list of text): Variables of the return period
    dataset to read; where there are several, the highest flow is used. Default:
    every variable with `river_id` and `return_period` dimensions (for
    GEOGLOWS, `gumbel`, `gumbel_hourly` and `gumbel_daily`).

* <a id="json-include_fdc"></a>`include_fdc` (true/false, default true): Add flow duration curve flows
    (`p_exceed_0`, `p_exceed_5`, ... `p_exceed_100` and `p_exceed_1`) to
    the flow statistics. GEOGLOWS, or `CUSTOM` with `fdc_file`.

* <a id="json-fdc_file"></a>`fdc_file` (text): Flow duration curves to use instead of GEOGLOWS', read
    like `return_period_file`, with a `p_exceed` dimension (in percent) or
    `p_exceed_<P>` columns for every exceedance listed under `include_fdc`.
    Unlike GEOGLOWS' curves, which fall back to the daily flows when they can't
    be read, a file that can't be read is an error.

* <a id="json-fdc_variable"></a>`fdc_variable` (text): Variable of the flow duration curve dataset to read.
    Default: `hourly_annual`, or the dataset's only variable.

* <a id="json-reanalysis_storage_options"></a>`reanalysis_storage_options` (dictionary): Storage options (fsspec) for
    reading a remote `return_period_file` or `fdc_file`, e.g.
    `{"profile": "my-aws-profile"}` for a private S3 bucket. Default: anonymous
    S3.

* <a id="json-raise_errors_if_river_ids_missing"></a>`raise_errors_if_river_ids_missing` (true/false, default true): What to do
    when some reaches aren't in the return period or flow duration curve
    datasets, e.g. near a regional dataset's edge: stop with an error, or (false)
    leave those reaches out.

* <a id="json-specified_bathyflow_field"></a>`specified_bathyflow_field` (text, default `rp2`): The flow statistic used
    as each reach's baseflow: ARC estimates the bathymetry for it, and
    `q_baseflow_threshold` filters by it. Any `p_exceed_<P>`, `rp<N>`,
    `p_exceed_0_premium` or `rp100_premium` (only `rp` ones for NWM), or any
    column of a `reanalysis_file`.

* <a id="json-specified_highflow_field"></a>`specified_highflow_field` (text, default `rp100_premium`): The flow
    statistic of the highest flow ARC builds rating curves for. Same choices.

* <a id="json-q_baseflow_threshold"></a>`q_baseflow_threshold` (number): Leave out reaches whose baseflow is below
    this, in m³/s.

## Forecasts

* <a id="json-forensic_forecast_date"></a>`forensic_forecast_date` (text): Map the forecast issued on this date,
    `YYYYMMDD` in UTC, e.g. `"20250807"`. Default: the newest forecast.
    GEOGLOWS forecasts are archived back to July 1, 2024. When a forecast can't be
    read, the retrospective flow on that date is used instead.

* <a id="json-forensic_forecast_hour"></a>`forensic_forecast_hour` (text): Hour (UTC) of a past NWM forecast:
    `00` to `23` for short range, `00`, `06`, `12` or `18` for medium
    range, `00` for long range.

* <a id="json-remove_old_forecast_files"></a>`remove_old_forecast_files` (true/false, default false): Delete this DEM's
    forecast flood maps, and files made from them, once their forecast is
    `age_of_forecast_days` old. The forecast being mapped is kept.

* <a id="json-age_of_forecast_days"></a>`age_of_forecast_days` (integer, default 7): Age in days at which
    `remove_old_forecast_files` deletes a forecast flood map.

## Land cover and roughness

NenCarta takes land cover from ESA WorldCover (2021) and gives each class a
Manning's n.

* <a id="json-land_cover_cache"></a>`land_cover_cache` (list of paths): Local ESA WorldCover tiles to use
    instead of downloading them, named by tile, e.g. `N36W120.tif`.

* <a id="json-land_watervalue"></a>`land_watervalue` (integer, default 80): The land cover value of open
    water (80 in ESA WorldCover).

* <a id="json-mannings_text_file"></a>`mannings_text_file` (path): A tab-separated table of Manning's n for each
    land cover value (columns `LC_ID`, `Description`, `Manning_n`). Default:
    NenCarta's table for ESA WorldCover.

## Bathymetry

DEMs show the water surface, not the river bed. ARC estimates the channel below
the water surface (the bathymetry) for each stream cell, and the flood mapper
puts it into the DEM the flood maps are made on (see
[Bathymetry](methods.md#bathymetry)).

* <a id="json-disable_bathymetry"></a>`disable_bathymetry` (true/false, default false): Skip the bathymetry and
    map floods on the DEM as it is.

* <a id="json-clean_dem"></a>`clean_dem` (true/false, default false): Smooth the stream channels with
    the DEM cleaner first. It sets the stream cells' elevations to fall steadily
    between reach ends and confluences, and spreads them across the channel.

* <a id="json-use_specified_depth_for_bathy_mask"></a>`use_specified_depth_for_bathy_mask` (true/false, default false): Give
    ARC, for its banks, the streams flooded to a set depth
    (`specify_depths_for_bathy_mask`; the `depth_mask` file) instead of the
    water mask, and outline the DEM cleaner's channels the same way: each cell
    is compared with the stream cells' heights plus the depth, averaged out to
    `depth_mask_distance` away. If false, the water mask
    (the streams and the water along them) outlines the DEM cleaner's channels
    and gives ARC its banks (see [methods](methods.md#the-channels-outline)). Either way, the flood mapper
    burns ARC's channel into the DEM as ARC wrote it, clipped to no outline, and
    `use_dem_derived_channel_mask` still steers the burn.

* <a id="json-specify_depths_for_bathy_mask"></a>`specify_depths_for_bathy_mask` (list of numbers, default `[0.1]`): The
    flood depths in metres for `use_specified_depth_for_bathy_mask`: one value,
    or two with `clean_dem`. The first is the DEM cleaner's initial flood map's;
    the last is ARC's water mask's. Even 0.1 m can cover a flat valley floor,
    since each cell's water surface is averaged from the stream cells as far as
    `depth_mask_distance` away.

* <a id="json-depth_mask_distance"></a>`depth_mask_distance` (number, default 2000): How far, in metres, each
    cell's water surface is averaged from the stream cells' when the streams
    are flooded to `specify_depths_for_bathy_mask` (Curve2Flood's
    `TopWidthPlausibleLimit` for those floods). The farther, the more of a flat
    valley floor the outline covers.

* <a id="json-area_km2_field"></a>`area_km2_field` (text, default `DSContArea_km2`): Stream attribute with
    each reach's drainage area in km², which ARC's drainage-area power laws take
    (`arc_args`' `coefficient_width`, `exponent_width`, `coefficient_depth` and
    `exponent_depth`). nencarta adds it to the streams from `area_m2_field`
    where it's missing.

* <a id="json-area_m2_field"></a>`area_m2_field` (text, default `DSContArea`): Stream attribute with
    drainage area in m², used when `area_km2_field` is missing.

* <a id="json-outlet_exchange"></a>`outlet_exchange` (`off`, `pass1` or `pass2`, default `off`): Start
    ARC's rivers that leave a buffered DEM from the water its neighbouring DEMs
    have there, instead of normal depth, which is often metres off; the error
    backs up tens of kilometres, and shows as a seam where two tiles' maps meet.
    Each DEM is run twice, in the same `output_dir` (or with the same
    `outlet_exchange_dir`) and with the same `buffer_distance`:

    - `pass1` makes the DEM's inputs, then runs ARC on only the reaches its
      neighbours start their outlets from: those crossing the lines where the
      neighbours' buffers end inside the DEM, every reach below them, and the
      reaches flowing straight into those (their beds set the beds below). It
      writes their water there to `<outlet_exchange_dir>/<DEM>_boundary_wse.parquet`
      and stops: no full ARC run, no flood maps.
    - `pass2`, once every DEM has done `pass1`, gathers the water of the
      neighbours' files that falls inside the DEM's buffer into the DEM's
      `outlet_wse` file (ARC's `Outlet_WSE_File`), then maps the DEM as usual.
      An outlet with no neighbour's water nearby (none ran, or it failed) starts
      from normal depth.

    Run `pass2` with `overwrite` false, so that it uses the inputs `pass1`
    made. The water at the neighbours' points comes within centimetres of a full
    ARC run's. Needs `buffer`, and `Standard_Step` in `arc_args`.

* <a id="json-outlet_exchange_dir"></a>`outlet_exchange_dir` (path): The folder the DEMs' `outlet_exchange`
    passes share. Empty: `output_dir/outlet_exchange`.

* <a id="json-arc_args"></a>`arc_args` (dictionary): ARC's own settings, by their keys in ARC's input
    file (see ARC's [model input file](https://github.com/RickytheGuy/automated-rating-curve-byu/blob/arc_overhaul/docs/model_input_file.md)),
    for both of ARC's runs: the DEM cleaner's and the main one. They are merged
    over nencarta's starting values: a key given replaces nencarta's whatever
    its case, and a key set to `null` takes ARC's default. See
    [The models' own settings](#the-models-own-settings) for nencarta's values,
    the keys nencarta sets itself, and the few it reads. Its old name,
    `bathy_args`, still works.

## Outputs

* <a id="json-make_vdt"></a>`make_vdt` (true/false, default true): Write ARC's VDT database: velocity,
    depth and top width of each stream cell at a range of flows. The flood maps
    are made from it, or from the curve file without it; the
    `Curve2Flood-FLDPLNpy` mapper needs it.

* <a id="json-make_curvefile"></a>`make_curvefile` (true/false, default true): Write ARC's curve file: a
    rating curve for each stream cell. The FIST inputs are made from it.

* <a id="json-make_ap_database"></a>`make_ap_database` (true/false, default true): Write ARC's area-perimeter
    database.

* <a id="json-make_cross_section_file"></a>`make_cross_section_file` (true/false, default false): Write ARC's cross
    sections.

* <a id="json-make_representative_cross_section_file"></a>`make_representative_cross_section_file` (true/false, default false):
    Write a representative cross section for each reach, averaged from its cross
    sections, for routing models.

* <a id="json-vdt_file_extension"></a>`vdt_file_extension` (text, default `txt`): File type of the VDT and
    area-perimeter databases: `txt`, `csv` or `parquet`.

* <a id="json-make_depth_maps"></a>`make_depth_maps` (true/false, default true): Write flood depth maps.

* <a id="json-make_velocity_maps"></a>`make_velocity_maps` (true/false, default true): Write flood velocity maps.

* <a id="json-make_wse_maps"></a>`make_wse_maps` (true/false, default true): Write water surface elevation
    maps.

* <a id="json-floodmap_identifier"></a>`floodmap_identifier` (text): Text added to the flood map file names, to
    tell runs apart.

* <a id="json-overwrite_floodmaps"></a>`overwrite_floodmaps` (true/false, default true): Remake flood maps that
    already exist.

* <a id="json-make_fist_inputs"></a>`make_fist_inputs` (true/false, default true): Write inputs for FIST (Flood
    Inundation Surface Topology): GeoJSON points of the water surface elevation
    at each stream cell, for each forecast (median, minimum and maximum) or each
    flow column of a user flow file, made from ARC's curve file
    (`make_curvefile`). The points where the streams with flows start are
    marked as SEED points: the upstream ends of the headwater streams (kept in
    the `Seed` file), and the upstream end of each stream whose inflowing
    streams have no flows. Not made in `return_period` mode.

* <a id="json-estimate_consequences"></a>`estimate_consequences` (true/false, default false): Estimate the damage to
    buildings in each depth map with Go-Consequences and the National Structure
    Inventory. Needs Docker and the `go-consequences` image (see
    [Installation](installation.md)), and the internet for the structures; CONUS only. Turns
    on `make_depth_maps`. Go-Consequences works in feet, so each depth map is
    given to it in feet and WGS84 (`<depth map>_WGS84_ft.tif`), and the depths
    in its GeoPackages are in feet. Without Docker on the PATH, no damage is
    estimated, and the log says so. When Go-Consequences can't get the
    structures from the NSI, the log says so too: its GeoPackage then lacks
    some or all of them.

* <a id="json-folder_paths"></a>`folder_paths` (dictionary): Where each output folder goes, as a path
    template per folder: `DEM`, `ARC_InputFiles`, `FloodMap`,
    `Bathymetry`, `DEM_Updated`, `STRM`, `LAND`, `FLOW`, `VDT`,
    `FIST`, `Consequences`, `FlowDirection` and `FLDPLN`, or `default`
    for the rest. Templates can use `{output_dir}`, `{name}`, `{dem}` (the
    DEM's name) and `{folder}`; relative paths are inside `output_dir`. For
    example, `{"FloodMap": "/results/{dem}"}` sends every DEM's flood maps to
    one tree. Keep `{name}` or `{dem}` in a template when several watersheds
    or DEMs share `output_dir`. Default: `{output_dir}/{name}/{folder}`.

* <a id="json-short_file_names"></a>`short_file_names` (true/false, default false): Name files by what they are
    alone, e.g. `VDT/VDT_Database_Bathy.txt` instead of
    `VDT/GEOGLOWS_<DEM>_VDT_Database_Bathy.txt`. Each DEM then needs its own
    folders: put `{dem}` in `folder_paths` when `dem_dir` holds several
    DEMs.

* <a id="json-file_names"></a>`file_names` (dictionary): New names for kinds of files, keyed by the part
    of the name that says what the file is, e.g. `{"VDT_Database_Bathy":
    "vdt"}`. The prefix, any return period or forecast part, and the extension
    are kept, so `{"ARC_Flood": "flood"}` names the return period maps
    `..._flood_rp100.tif`; an empty name leaves just that part, e.g.
    `rp100.tif`. The kinds are `dem`, `fixed`, `filled`, `fixed_Clean`,
    `Clean`, `flowdir`, `StrmShp`, `StrmShp_relocated`,
    `STRM_Raster_Clean`, `matched`,
    `lakes`, `stream_info`, `fldpln_library`, `LAND_Raster`,
    `AR_Manning_n_MED`, `Reanalysis`, `2yr_flow_initial`, `rp`,
    `forecast`, `Flow_COMID_Q`, `ARC_Input_Bathy`,
    `ARC_Input_InitialFlood`, `ARC_Input_FloodForecast`, `ARC_Input`,
    `VDT_Database_Initial`, `VDT_Database_Bathy`, `AP_Database_Bathy`,
    `CurveFile_Initial`, `CurveFile_Bathy`, `XS`, `Representative_XS`,
    `Mapper_Input_Bathy`, `Mapper_Input_InitialFlood`, `Mapper_Input_DepthMask`,
    `water_mask`, `depth_mask`, `ARC_Bathy`, `FS_Bathy`, `Reanalysis_Boundary`,
    `ARC_Input_Boundary`, `VDT_Database_Boundary`, `ARC_Bathy_Boundary`,
    `outlet_wse`, `ARC_Flood`, `ARC_Flood_Initial`, `ARC_FloodDepth`,
    `ARC_FloodWSE`, `ARC_FloodVEL`, `Seed` and `FIST`. Two files can't end up
    with the same path.

* <a id="json-use_parquet"></a>`use_parquet` (true/false, default false): Write the FLDPLN stream info
    table as Parquet instead of CSV.

* <a id="json-use_yaml"></a>`use_yaml` (true/false, default false): Write the ARC and Curve2Flood input
    files as YAML instead of text.

* <a id="json-compression"></a>`compression` (text, default `LZW`): Compression of the rasters NenCarta
    writes itself, e.g. `LZW`, `DEFLATE` or `ZSTD`. The burned and filled DEMs are written with the
    floating point predictor and, with `ZSTD`, at level 1, which writes them several times faster and
    smaller than GDAL's default level.

## Flood mapper

* <a id="json-curve2flood_args"></a>`curve2flood_args` (dictionary): Curve2Flood's own settings for the
    flood maps, by their keys in its [input file](https://github.com/RickytheGuy/curve2flood/blob/fldpln-fast-faithful/docs/input_file.md),
    such as `TW_MultFact`, `Flood_Bathy_Mask` or `FLDPLN_Min_Flood_Depth`. They
    are merged over nencarta's starting values as `arc_args` are over its
    (see [The models' own settings](#the-models-own-settings)). Its old name,
    `floodmap_args`, still works.

* <a id="json-fldpln_library_args"></a>`fldpln_library_args` (dictionary): Arguments of Curve2Flood's
    `build_fldpln_library`, which builds the Curve2Flood-FLDPLNpy library (see
    Curve2Flood's [FLDPLN Mapper](https://github.com/RickytheGuy/curve2flood/blob/fldpln-fast-faithful/docs/fldpln.md)), such as `solver`, `dh`,
    `spill_decay` or `max_cells_per_segment`. nencarta gives none of its own,
    so the function's defaults hold. Not `parallel` with nencarta's `parallel`.

* <a id="json-mapper"></a>`mapper` (text, default `Curve2Flood-Kernel Weighted`): The flood mapper:
    `Curve2Flood-Kernel Weighted`, `Curve2Flood-FLDPLNpy`,
    `Curve2Flood-Multi-Point Interpolation` or `FloodSpreader` (see
    [Flood maps](methods.md#flood-maps)). FLDPLNpy floods along the DEM's flow paths and
    needs `move_stream_network_to_thalweg`. FloodSpreader needs a
    `floodspreader.py` script, which isn't part of NenCarta, in the installed
    `nencarta` package folder.

`fldpln_library_args` are for `Curve2Flood-FLDPLNpy`, which first builds a
library of the floodplain cells each stream cell floods, and at what depth.

## Running

* <a id="json-overwrite"></a>`overwrite` (true/false, default false): Remake intermediate files (DEMs,
    streams, flows, ARC outputs, inputs) that already exist. When false, they are
    reused, so a second run only redoes what is missing.

* <a id="json-parallel"></a>`parallel` (true/false, default false): Process several DEMs at once.

* <a id="json-num_workers"></a>`num_workers` (integer): Number of processes when `parallel`.

* <a id="json-profile"></a>`profile` (true/false, default true): Print how long each step took (when
    not `parallel`).

* <a id="json-quiet"></a>`quiet` (true/false, default false): Hide ARC's and Curve2Flood's progress
    output.

* <a id="json-raise_errors_if_nothing_in_domain"></a>`raise_errors_if_nothing_in_domain` (true/false, default true): Stop with an
    error when a DEM has no streams or source data. If false, skip that DEM.

## The models' own settings

ARC and Curve2Flood document their own settings, so nencarta passes them on
rather than naming each one. They come in three dictionaries: `arc_args` for
both of ARC's runs, `curve2flood_args` for the flood maps, and
`fldpln_library_args` for `build_fldpln_library`. Each model gets an input
file of its own: ARC's and Curve2Flood's each stop at a key that isn't theirs.

Each dictionary is merged over nencarta's starting values: a key given replaces
nencarta's whatever its case, and a key set to `null` takes the model's own
default. When nencarta starts, each model checks the keys and their values
(ARC's and Curve2Flood's `check_keys`, and the function's arguments), so a
misspelt key or a wrong value stops it before anything runs.

| Dictionary | Key | nencarta's value | The model's default |
| --- | --- | --- | --- |
| `arc_args` | `VDT_Database_NumIterations` | 30 | 15 |
| `arc_args` | `Bathy_Trap_H` | 0.2 | 0.2 |
| `arc_args` | `Degree_Manip` | 6.1 | 1.1 |
| `arc_args` | `Degree_Interval` | 1.5 | 1.0 |
| `arc_args` | `Low_Spot_Range` | 2 | 0 |
| `arc_args` | `Gen_Dir_Dist` | 10 | 10 |
| `arc_args` | `Gen_Slope_Dist` | 10 | 10 |
| `arc_args` | `Stream_Slope_Method` | `local_average_corrected` | `local_average_corrected` |
| `arc_args` | `FindBanksBasedOnLandCover` | true | false |
| `arc_args` | `Slope_Low_Percentile` | 25 | 5 |
| `arc_args` | `Slope_High_Percentile` | 75 | 95 |
| `arc_args` | `coefficient_width` | 2.44 | none |
| `arc_args` | `exponent_width` | 0.34 | none |
| `curve2flood_args` | `Make_Output_GPKG` | true | true |
| `curve2flood_args` | `TW_MultFact` | 1.5 | 3.0 |
| `curve2flood_args` | `TopWidthPlausibleLimit` | 6000 | 1000.0 |

nencarta sets the files and fields itself, so they can't be given: ARC's and
Curve2Flood's input and output files, `BathyWaterMask`, ARC's `reach_id`,
`downstream_reach_id` and `drainage_area_field`, `Flow_File_ID`,
`Flow_File_BF` and `Flow_File_QMax`, the land cover's water
(`LC_Water_Value` for ARC, `LAND_WaterValue` for Curve2Flood, both
`land_watervalue`), Curve2Flood's `mapper` (nencarta's `mapper`) and
`Set_Depth`, and the files, masks and progress bar `build_fldpln_library` gets.
It reads a few of the settings itself:

* `Standard_Step` (`arc_args`): ARC then gets the reach ids, with or without
    bathymetry, and `outlet_exchange` needs it.
* `coefficient_width`, `exponent_width`, `coefficient_depth` and
    `exponent_depth` (`arc_args`): ARC gets the drainage area field with them,
    the coefficients scaled to take m² where the streams have only
    `area_m2_field`. Without bathymetry, or without the area, ARC gets none.
* `Bathy_Use_Banks` (`arc_args`): Curve2Flood burns ARC's bathymetry into the
    DEM as ARC estimated it.
* `Flood_WaterLC_and_STRM_Cells` and `Flood_Bathy_Mask` (`curve2flood_args`):
    the flood maps get the land cover, and the water mask, which nencarta then
    makes for them.
* `parallel` (`fldpln_library_args`): not with nencarta's own `parallel`.

nencarta's options that were the models' own settings now stop it with an
error naming where each went:

| Old option | Now |
| --- | --- |
| `bathy_use_banks` | arc_args' Bathy_Use_Banks |
| `find_banks_based_on_landcover` | arc_args' FindBanksBasedOnLandCover |
| `use_power_laws_for_bathymetry` | arc_args' coefficient_depth and exponent_depth (ARC uses the depth's power law when they are given) |
| `coefficient_depth` | arc_args' coefficient_depth |
| `exponent_depth` | arc_args' exponent_depth |
| `coefficient_width` | arc_args' coefficient_width |
| `exponent_width` | arc_args' exponent_width |
| `slope_low_percentile` | arc_args' Slope_Low_Percentile |
| `slope_high_percentile` | arc_args' Slope_High_Percentile |
| `create_reach_average_curve_file` | arc_args' Reach_Average_Curve_File |
| `standard_step` | arc_args' Standard_Step |
| `valley_sections` | arc_args' Valley_Sections |
| `valley_dir_widths` | arc_args' Valley_Dir_Widths |
| `valley_dir_dist` | arc_args' Valley_Dir_Dist |
| `drop_edge_sections` | arc_args' Drop_Edge_Sections |
| `edge_margin` | arc_args' Edge_Margin |
| `flood_waterlc_and_strm_cells` | curve2flood_args' Flood_WaterLC_and_STRM_Cells |
| `flood_bathy_mask` | curve2flood_args' Flood_Bathy_Mask |
| `fldpln_min_flood_depth` | curve2flood_args' FLDPLN_Min_Flood_Depth |
| `fldpln_dh` | fldpln_library_args' dh |
| `fldpln_min_depth` | fldpln_library_args' fldmn |
| `fldpln_max_depth` | fldpln_library_args' fldmx |
| `fldpln_spill_decay` | fldpln_library_args' spill_decay |
| `fldpln_max_cells_per_segment` | fldpln_library_args' max_cells_per_segment |
| `fldpln_keep_spilling` | fldpln_library_args' iterative_spill |
| `fldpln_solver` | fldpln_library_args' solver |
| `fldpln_parallel` | fldpln_library_args' parallel |

`bathy_args` and `floodmap_args`, the old names of `arc_args` and
`curve2flood_args`, still work, and the new ones are merged over them when
both are given. `bathy_args` keeps its old meaning: it replaces
nencarta's whole set of the keys it had by default but for
`VDT_Database_NumIterations` and `Bathy_Trap_H`, its `TopWidthPlausibleLimit`
becomes `depth_mask_distance`, and its `TW_MultFact` and `Make_Output_GPKG`,
which changed nothing, are left out.

## Environment variables

* `NWM_API_KEY`: The NWM API key, when `nwm_api_key` isn't given.
* `NENCARTA_CACHE_DIR`: Where NenCarta caches raster and vector information
    (default: `~/.cache`). The cache is skipped if it can't be used.
* `NUMBA_CACHE_DIR`: Where Numba caches compiled code, e.g. when the default
    location isn't writable. Keep it somewhere that lasts between runs: with an
    empty cache, preparing a 1 degree tile's inputs spends about 30 s more
    compiling, before ARC and Curve2Flood compile their own.

## Output folders

Each DEM's outputs go in `<output_dir>/<name>`, one folder per kind:

* `DEM`: the DEM cut from `source_dems` (with `bbox` or `buffer`).
* `LAND`: the land cover raster and the Manning's n table.
* `STRM`: the domain's streams (`StrmShp`), their raster
    (`STRM_Raster_Clean`), and when moving streams the moved network
    (`matched`, vector and raster) and the lakes raster.
* `FLOW`: the flow statistics (`Reanalysis.csv`), the baseflows, and the
    forecast or return period flows. With `outlet_exchange` `pass1`, the flow
    statistics of the reaches its ARC run rates (`Reanalysis_Boundary`).
* `DEM_Updated`: the burned (`fixed`), filled and cleaned (`Clean`) DEMs.
* `FlowDirection`: the D8 flow directions.
* `ARC_InputFiles`: ARC's input files (`ARC_Input_Bathy`, and the DEM
    cleaner's `ARC_Input_InitialFlood`) and Curve2Flood's, each model's its
    own: the flood maps' (`ARC_Input`), the one that burns ARC's bathymetry into
    the DEM (`Mapper_Input_Bathy`), and with `use_specified_depth_for_bathy_mask`
    the ones that flood the streams to the set depths (`Mapper_Input_DepthMask`,
    and the DEM cleaner's `Mapper_Input_InitialFlood`); with `outlet_exchange`,
    `pass1`'s ARC input file (`ARC_Input_Boundary`) and the neighbours' water
    `pass2` starts the outlets from (`outlet_wse`).
* `VDT`: ARC's VDT and area-perimeter databases, curve files and cross
    sections; with `outlet_exchange` `pass1`, its run's VDT database
    (`VDT_Database_Boundary`).
* `Bathymetry`: the water mask (and with `use_specified_depth_for_bathy_mask`,
    ARC's water mask, `depth_mask`), ARC's bathymetry (`ARC_Bathy`) and the DEM
    with bathymetry the flood maps are made on (`FS_Bathy`).
* `FLDPLN`: the stream info table and the FLDPLN library.
* `FloodMap`: the flood maps.
* `FIST`: the FIST inputs, and the `Seed` file of the headwater streams'
    upstream ends.
* `Consequences`: the Go-Consequences inputs and results (depths in feet). The
    depth maps it reads, in feet and WGS84, are beside the flood maps
    (`_WGS84_ft`).

With `outlet_exchange`, the DEMs also share a folder, `outlet_exchange_dir`
(`<output_dir>/outlet_exchange` by default), where `pass1` writes each DEM's
water near its edges for its neighbours (`<DEM>_boundary_wse.parquet`).

Files are named `<source>_<DEM>_<kind>` or `<DEM>_<kind>`, where `<source>`
is `GEOGLOWS` or `NWM` (see `folder_paths`, `short_file_names` and
`file_names` to change this). The flood maps are named after their flows:

* `ARC_Flood`: flood extent, also as a GeoPackage unless `curve2flood_args` sets
    `Make_Output_GPKG` to false.
* `ARC_FloodDepth`, `ARC_FloodWSE`, `ARC_FloodVEL`: depth, water surface
    elevation and velocity.

followed by the forecast's date (and hour, for NWM), the flow file's name, or
the return period, e.g. `GEOGLOWS_<DEM>_ARC_Flood_rp100.tif`.
