Configuration Reference
=======================

JSON Inputs
--------------

Below is a description of all the JSON inputs that NenCarta accepts when you run the
``json`` subcommand. Each watershed definition is a JSON object with the required
keys and optional processing parameters described above. You can include multiple
watershed objects in the ``watersheds`` array to run them in batch mode.

* ``age_of_forecast_days`` (Integer, optional): This is how old previous forecasts
  will be allowed to be based upon the current day. For example, specifying 7 days
  will delete all forecast flood inundation maps that are older than 7 days. Default
  7.

* ``bathy_args`` (Dictionary, optional): A dictionary of arguments that will be
  passed to ARC and the flood mapper when estimating bathymetry in each cross-section.
  See the ARC and the respective flood mapper documentation for more information on
  these arguments.

* ``bathy_use_banks`` (Bool, optional): Setting this to True will allow the system
  to use estimated bank elevations to estimate bathymetry using the streamflow
  specified in specified_bathyflow_field. Setting this value to False will allow the
  system to use the estimated water surface elevation to estimate bathymetry the using
  the streamflow specified in specified_bathyflow_field. Default False.

* ``clean_dem`` (Bool, optional): Setting this value to True will allow the system to
  use the DEM Cleaner. Setting this value to False will bypass use of the DEM Cleaner.
  Default False.

* ``create_reach_average_curve_file`` (Bool, optional): Setting this value to true
  instructs NenCarta to direct ARC to output the curve file by taking velocity, depth,
  and top-width estimates for all stream cells on an individual stream reach and
  creating the curve parameters for each stream cell. Setting this value to False
  instructs NenCarta to direct ARC to output the curve file using velocity, depth, and
  top-width estimates for each stream cell. Default False.

* ``dem_dir`` (String): A full filepath to the directory containing one or more DEMs
  that you will be using as input in NenCarta.

* ``dem_filter`` (String, optional): A glob string with which files in the
  ``dem_dir`` must match to be included in the run. By default, "*", or all files.

* ``disable_bathymetry`` (Bool, optional): Setting this equal to true will disable
  the estimation of bathymetry within the hydraulic calculations and the creation of a
  composite topobathymetric surface.

* ``estimate_consequences`` (Bool, optional): Setting this equal to true will utilize
  Go-Consequences and the National Structure Inventory (NSI) to perform consequence
  assessment for the area within each flood inundation map. This functionality is
  currently only available for the coterminous United States. Default False.

* ``find_banks_based_on_landcover`` (Bool, optional): Setting this value to True will
  direct NenCarta to first try finding the banks using the land cover. If the stream
  cell is in a water pixel, NenCarta will direct ARC to search for the end of the
  water surrounding the stream cell and this will location will be designated as the
  banks. Setting this value to False will direct NenCarta and ARC to find the banks by
  assuming the channel is flat in the DEM and allowing ARC to find the banks when the
  flat water surface ends in the cross-section. Default True.

* ``flood_waterlc_and_strm_cells`` (Bool, optional): Argument for the flood mapper,
  whether to force in the flood map all cells that are wet in the land cover and
  stream raster as wet in the flood map. Default False.

* ``floodmap_args`` (Dictionary, optional): A dictionary of arguments that will be
  passed to the flood mapper when creating flood inundation maps. See the respective
  documentation for more information on these arguments.

* ``floodmap_identifier`` (String, optional): A string that will be appended to the
  flood map filenames to help identify the flood maps created by this NenCarta
  simulation. Default is an empty string.

* ``floodmap_mode`` (Bool, optional): Either "forecast" or "user". Forecast mode
  will run either a GEOGLOWS or NWM forecast flows. User mode allows the option
  ``user_flow_files`` to be populated and used instead. Default "forecast".

* ``flowline`` (String): A full filepath to the flowline shapefile that you'll be
  using to run NenCarta.

* ``forensic_forecast_date`` (String, optional): If you want to use a past forecast,
  you can specify the date here in YYYYMMDD format (e.g., "20250807" is August 7,
  2025) in UTC. The archive of GEOGLOWS forecasts goes back to July 1, 2024. We are
  unsure of the current limitations for the National Water Model archive of forecasts.

* ``forensic_forecast_hour`` (String, optional): If you want to use a past National
  Water Model forecast, along with the forensic_forecast_date, you must also specify a
  forensic forecast hour. This is the hour the forecast was produced, in UTC. For the
  "NWM_short_range" forecast, the forensic forecast hour must be between 0 and 23,
  expressed as a string (e.g., "00"). For the "NWM_medium_range" forecast, the
  forensic forecast hour must be "00", "06", "12", or "18". For the
  "NWM_long_range" forecast, the forensic forecast hour must be "00".

* ``geoglows_vpu`` (Integer, optional): Only used for CONUS Vector Processing Units
  (VPUs) in GEOGLOWS. When the NenCarta user wants to use the GEOGLOWS map-tables to
  identify where flooding is forecast in CONUS, the GEOGLOWS VPU can be specified in
  this option. NenCarta only requires that the streamline vector for the VPU be
  available in this use case. NenCarta will use the location of flowlines forecasts to
  meet or exceed the 2-year discharge to both download USGS 3DEP DEM data and simulate
  flood inundation.

* ``lake_filter_json`` (String, optional): The path to the GEOGLOWS json that
  describes which stream reaches are within a lake. Optional input. This is currently
  only functional for GEOGLOWS data.

* ``land_watervalue`` (Int, optional): The value in the land cover raster that
  represents water. Default 80.

* ``make_ap_database`` (Bool, optional): Whether ARC will make an Area-Perimeter
  file. Default True.

* ``make_curvefile`` (Bool, optional): Whether ARC will make a Curve file. Default
  True.

* ``make_depth_maps`` (Bool, optional): Whether or not to make flood depth maps.
  Default True.

* ``make_fist_inputs`` (Bool, optional): Whether or not to make FIST inputs for
  flood. Default True.

* ``make_velocity_maps`` (Bool, optional): Whether or not to make flood velocity
  maps. Default True.

* ``make_wse_maps`` (Bool, optional): Whether or not to make flood water surface
  elevation maps. Default True.

* ``mannings_text_file`` (String, optional): The full filepath to the Manning's n
  text file to be used in the flood mapping process. If not provided, NenCarta will
  use its built-in default Manning's n values.

* ``mapper`` (String, optional): Here you're specifying if you're running
  "FloodSpreader", "Curve2Flood-Kernel Weighted", or "Curve2Flood-FLDPLNpy", or
  "Curve2Flood-Multi-Point Interpolation" when performing bathymetry estimation and
  flood inundation mapping. Defaults to FloodSpreader

* ``min_match_score`` (Float, optional): This is the threshold value that is used to
  determine if a good match is made when using the
  ``move_stream_network_to_new_locations`` option. NenCarta scores the match between
  each new stream and old stream by creating a 50 m buffer around each old and new
  stream and determining what proportion of the two areas overlap. The
  ``min_match_score`` is the minimum proportion that can be considered a credible
  match by the system.

* ``move_stream_network_to_new_locations`` (Bool, optional): If True, this option
  will allow NenCarta to use Whitebox to create a new stream network and attempt to
  conflate the existing network with the new terrain derived network. Default is
  False. This is required if using FLDPLNpy as the ``mapper`` option. This option
  should not be used if your DEM does not contain the entire contribution upstream
  area of your area of interest.

* ``name`` (String): The name of the watershed you're modeling.

* ``new_strm_threshold_km2`` (Float, optional): This value represents the
  contributing area used to create a new stream network with the DEM when enabling the
  ``move_stream_network_to_new_locations`` option as True. The value is in square km
  and is required when ``move_stream_network_to_new_locations`` is True.

* ``nwm_api_key`` (String, optional): Required when streamflow_source is set to "NWM"
  or any "NWM_*" option. This is the NWM API key passed as the 'x-api-key' header for
  NWM requests. You must apply for an API key using these instructions:
  https://docs.ciroh.org/docuhub-staging/docs/products/data-management/bigquery-api/

* ``output_dir`` (String): The full filepath to the directory where your output will
  be saved.

* ``overwrite_floodmaps`` (Bool, optional): Whether to replace existing flood
  indunation, depth, WSE, etc. maps if they exist. Defaults to True.

* ``process_stream_network`` (Bool, optional): Setting this value to True will direct
  the forecast system to take the flowline network (your flowline variable) and
  determine which flowlines are within each of your DEM tiles and download the
  necessary ECMWF GEOGLOWS Streamflow Service historic data for each DEM tile. Setting
  this value to False will bypass the creation of these system inputs and assumes that
  those inputs already exist. Default False.

* ``q_baseflow_threshold`` (Float, optional): Setting this value (in cubic meters per
  second) equal to a float value will filter out all streams in your domain that have
  a baseflow (from ``specified_bathyflow_field``) that are less than the specified
  value. Default is None.

* ``quiet`` (Bool, optional): Setting this value to True will suppress ARC and
  Curve2Flood output. Default False.

* ``remove_old_forecast_files`` (Bool, optional): If True, check existing flood maps
  and see if they have forecast data older than ``age_of_forecast_days``. If so,
  remove. Default True.

* ``specified_bathyflow_field`` (String, optional): The field in the GEOGLOWS
  downloaded reanalysis data that will be provided to ARC to estimate bathmetry in
  each cross-section. For "GEOGLOWS" it must be one of"p_exceed_0", "p_exceed_5",
  "p_exceed_10", "p_exceed_15", "p_exceed_20", "p_exceed_25", "p_exceed_30",
  "p_exceed_35", "p_exceed_40", "p_exceed_45", "p_exceed_50", "p_exceed_65",
  "p_exceed_70", "p_exceed_75", "p_exceed_80", "p_exceed_85", "p_exceed_90",
  "p_exceed_95", "p_exceed_100", "rp2", "rp5", "rp10", "rp25", "rp50",
  "rp100","p_exceed_0_premium", or "rp100_premium". For "NWM" it must be one of
  "rp2", "rp5", "rp10", "rp25", "rp50", "rp100", or "rp100_premium". Default is
  "p_exceed_50".

* ``specify_depths_for_bathy_mask`` (List of Numbers, optional): If
  ``use_specified_depth_for_bathy_mask`` is True, the user must specify at least one
  depth value for FloodSpreaderPy to use when creating a water mask for the DEM
  cleaner and the bathymetry estimation processes. If ``clean_dem`` is False, this
  argument requires only one float value and that value will be used to create the
  bathymetry estimation water mask. If ``clean_dem`` is True, you will need to
  specify two float values, the first value is the depth used to create the water mask
  for the DEM cleaner and the second value is used to create the bathymetry estimation
  water mask. The json processes accepts these inputs as a Python list. The command
  line interface accepts these as one or two separate values (i.e.,
  --specify_depths_for_bathy_mask 1.0 2.0).

* ``specified_highflow_field`` (String, optional): The field in the GEOGLOWS
  downloaded reanalysis data that will be provided to ARC as the highest flow used to
  estimate water surface elevation. For "GEOGLOWS" it must be one of "p_exceed_0",
  "p_exceed_5", "p_exceed_10", "p_exceed_15", "p_exceed_20", "p_exceed_25",
  "p_exceed_30", "p_exceed_35", "p_exceed_40", "p_exceed_45", "p_exceed_50",
  "p_exceed_65", "p_exceed_70", "p_exceed_75", "p_exceed_80", "p_exceed_85",
  "p_exceed_90", "p_exceed_95", "p_exceed_100", "rp2", "rp5", "rp10", "rp25",
  "rp50", "rp100","p_exceed_0_premium", or "rp100_premium". For "NWM" it must be
  one of "rp2", "rp5", "rp10", "rp25", "rp50", "rp100", or "rp100_premium".
  Default is "rp100_premium".

* ``streamflow_source`` (String, optional): Setting this equal to "GEOGLOWS" will
  force NenCarta to use GEOGLOWS retrospective and forecast streamflow data. The
  deafult is GEOGLOWS. Setting this to "NWM_short_range" will force NenCarta to use
  the National Water Model retrospective and short-range forecast streamflow data.
  Setting this to "NWM_medium_range" will force NenCarta to use the National Water
  Model retrospective and medium-range forecast streamflow data. Setting this to
  "NWM_long_range" will force NenCarta to use the National Water Model retrospective
  and long-range forecast streamflow data. Default "GEOGLOWS".

* ``StrmOrder_Field`` (String, optional): The field in the flowline GIS data that
  specifies the stream order of the streams in your model domain. This input is
  required if you plan to use StrmOrder_Lower or StrmOrder_Upper to limit which
  streams will be used for flood inundation mapping by NenCarta.

* ``StrmOrder_Lower`` (Integer, optional): The lowest value of stream order that you
  plan to use in your NenCarta simulation.

* ``StrmOrder_Upper`` (Integer, optional): The highest value of stream order that you
  plan to use in your NenCarta simulation.

* ``use_specified_depth_for_bathy_mask`` (Bool, optional): Setting this value to
  False will direct NenCarta to create a water mask that is a compilation of the
  stream network raster and the land use designated as water. This will be used to
  clean the input DEM and burn bathymetry into the DEM. Setting this value to True
  will direct NenCarta to create a water mask for DEM cleaning that fills the stream
  cell with a specified depth of water. The depth of water will be based upon the
  value(s) specified in the ``specify_depths_for_bathy_mask`` argument. Default True.

* ``use_warning_flags_to_download_dem`` (Bool, optional): TODO. Default False.

* ``user_flow_files`` (String or List of Strings, optional): If ``floodmap_mode`` is
  "user", than use this file or list of files to create flood maps instead of looking
  at the forecast.

* ``vdt_file_extension`` (String, optional): The file extension of the VDT files to
  be created. Default "txt".

Required watershed keys
-----------------------

Each watershed definition requires these arguments in the JSON file. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* ``name``
* ``flowline``
* ``dem_dir``
* ``output_dir``

Common processing options
-------------------------

These options control the main flood-mapping workflow. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* ``process_stream_network``
* ``clean_dem``
* ``move_stream_network_to_new_locations``
* ``new_strm_threshold_km2``
* ``use_warning_flags_to_download_dem``

Forecast and flow options
-------------------------

These options select the streamflow source and forecast behavior. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* ``streamflow_source``: Forecast provider and product selection.
* ``forensic_forecast_date``: Use a prior forecast by date in ``YYYYMMDD`` format.
* ``forensic_forecast_hour``: Required for archived NWM forecasts.
* ``age_of_forecast_days``: Remove outdated forecast outputs based on age.
* ``remove_old_forecast_files``: Enable cleanup of stale forecast products.
* ``user_flow_files``: Provide user-supplied flow files when ``floodmap_mode`` is ``user``.

ARC options
-----------
These options control the ARC workflow that estimates bathymetry and creates curve files. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* ``create_reach_average_curve_file``
* ``make_curvefile``
* ``make_ap_database``
* ``bathy_args``
* ``vdt_file_extension``

Bathymetry options
------------------

These options control bathymetry estimation and flood-map generation. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* ``disable_bathymetry``
* ``bathy_use_banks``
* ``use_specified_depth_for_bathy_mask``
* ``specify_depths_for_bathy_mask``
* ``find_banks_based_on_landcover``


Flood-map options
-----------------
These options control the flood-map generation process. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* ``mapper``
* ``make_depth_maps``
* ``make_velocity_maps``
* ``make_wse_maps``
* ``floodmap_identifier``
* ``floodmap_args``

Consequence estimation
----------------------

Set ``estimate_consequences`` to ``true`` to invoke the Go-Consequences workflow after flood-map generation.
See `this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_ for more information on 
what ``estimate_consequences`` does.

Creating FIST inputs
----------------------

NenCarta can create inputs for the Flood Inundation Surface Topology (FIST) model as a forecast. To do this, set
``make_fist_inputs`` to ``true`` to generate FIST-ready inputs as part of the workflow.
NenCarta writes those files to the ``FIST`` subdirectory in the
``output_dir``/ ``name`` directory.

NenCarta relies on ARC to generate the FIST inputs. For the underlying ARC workflow, see
the `ARC-to-FIST documentation <https://github.com/MikeFHS/automated-rating-curve/wiki/Creating-Inputs-for-the-Flood-Inudation-Surface-Topology-(FIST)-Flood-Inundation-Mapping-Software>`_.

The generated FIST inputs are created for each forecast scenario, including minimum,
median, and maximum streamflow forecasts. They include stream-cell locations, water
surface elevations, and SEED values (0 = not a seed, 1 = a seed). SEED values designate 
the furthest upstream points for headwater streams and are used by FIST to define 
flow paths and inundation extents.

The FIST subdirectory contains the following file types:

* ``*_{forecast_date}_min.geojson``: GeoJSON point features for stream-cell locations,
  with water-surface elevation and SEED values for the minimum streamflow forecast.

* ``*_{forecast_date}_med.geojson``: GeoJSON point features for stream-cell locations,
  with water-surface elevation and SEED values for the median streamflow forecast.

* ``*_{forecast_date}_max.geojson``: GeoJSON point features for stream-cell locations,
  with water-surface elevation and SEED values for the maximum streamflow forecast.

* ``*_Seed.shp``: A shapefile containing the SEED locations that designate the furthest
  upstream points for headwater streams.
