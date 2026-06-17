Configuration Reference
=======================

JSON Inputs
--------------

Below is a description of all the JSON inputs that NenCarta accepts when you run the
``json`` subcommand. Each watershed definition is a JSON object with the required
keys and optional processing parameters described above. You can include multiple
watershed objects in the ``watersheds`` array to run them in batch mode.

.. _json-age_of_forecast_days:

* ``age_of_forecast_days`` (Integer, optional): This is how old previous forecasts
  will be allowed to be based upon the current day. For example, specifying 7 days
  will delete all forecast flood inundation maps that are older than 7 days. Default
  7.

.. _json-bathy_args:

* ``bathy_args`` (Dictionary, optional): A dictionary of arguments that will be
  passed to ARC and the flood mapper when estimating bathymetry in each cross-section.
  See the ARC and the respective flood mapper documentation for more information on
  these arguments. See the `ARC documentation <https://github.com/MikeFHS/automated-rating-curve/wiki/ARC-Input-File-Arguments>`_ 
  for more information on the arguments that can be passed to ARC. See the 
  `Curve2Flood documentation <https://github.com/MikeFHS/curve2flood/wiki/Curve2Flood-Input-File-Arguments>`_
  for more information on the arguments that can be passed to Curve2Flood.

.. _json-bathy_use_banks:

* ``bathy_use_banks`` (Bool, optional): Setting this to True will allow the system
  to use estimated bank elevations to estimate bathymetry using the streamflow
  specified in specified_bathyflow_field. Setting this value to False will allow the
  system to use the estimated water surface elevation to estimate bathymetry the using
  the streamflow specified in specified_bathyflow_field. Default False.

.. _json-clean_dem:

* ``clean_dem`` (Bool, optional): Setting this value to True will allow the system to
  use the DEM Cleaner. Setting this value to False will bypass use of the DEM Cleaner.
  Default False.

.. _json-create_reach_average_curve_file:

* ``create_reach_average_curve_file`` (Bool, optional): Setting this value to true
  instructs NenCarta to direct ARC to output the curve file by taking velocity, depth,
  and top-width estimates for all stream cells on an individual stream reach and
  creating the curve parameters for each stream cell. Setting this value to False
  instructs NenCarta to direct ARC to output the curve file using velocity, depth, and
  top-width estimates for each stream cell. Default False.

.. _json-dem_dir:

* ``dem_dir`` (String): A full filepath to the directory containing one or more DEMs
  that you will be using as input in NenCarta. If ``mapper`` is set to
  ``Curve2Flood-FLDPLNpy`` and a DEM is stored in geographic coordinates
  (latitude/longitude), NenCarta first creates a projected GeoTIFF copy in the
  watershed ``FlowDirection`` folder using ``WGS 84 / NSIDC EASE-Grid 2.0
  Global`` (EPSG:6933). That projected copy becomes the DEM used for all later
  processing for that tile.

.. _json-dem_filter:

* ``dem_filter`` (String, optional): A glob string with which files in the
  ``dem_dir`` must match to be included in the run. By default, "*", or all files.

.. _json-disable_bathymetry:

* ``disable_bathymetry`` (Bool, optional): Setting this equal to true will disable
  the estimation of bathymetry within the hydraulic calculations and the creation of a
  composite topobathymetric surface.

.. _json-estimate_consequences:

* ``estimate_consequences`` (Bool, optional): Setting this equal to true will utilize
  Go-Consequences and the National Structure Inventory (NSI) to perform consequence
  assessment for the area within each flood inundation map. This functionality is
  currently only available for the coterminous United States. Default False.

.. _json-find_banks_based_on_landcover:

* ``find_banks_based_on_landcover`` (Bool, optional): Setting this value to True will
  direct NenCarta to first try finding the banks using the land cover. If the stream
  cell is in a water pixel, NenCarta will direct ARC to search for the end of the
  water surrounding the stream cell and this will location will be designated as the
  banks. Setting this value to False will direct NenCarta and ARC to find the banks by
  assuming the channel is flat in the DEM and allowing ARC to find the banks when the
  flat water surface ends in the cross-section. Default True.

.. _json-flood_waterlc_and_strm_cells:

* ``flood_waterlc_and_strm_cells`` (Bool, optional): Argument for the flood mapper,
  whether to force in the flood map all cells that are wet in the land cover and
  stream raster as wet in the flood map. Default False.

.. _json-floodmap_args:

* ``floodmap_args`` (Dictionary, optional): A dictionary of arguments that will be
  passed to the flood mapper when creating flood inundation maps. See the respective
  documentation for more information on these arguments. See the 
  `ARC input-file documentation <https://github.com/MikeFHS/automated-rating-curve/wiki/ARC-Input-File-Arguments>`_ 
  for more information on the arguments that can be passed to ARC. See the 
  `Curve2Flood documentation <https://github.com/MikeFHS/curve2flood/wiki/Curve2Flood-Input-File-Arguments>`_
  for more information on the arguments that can be passed to Curve2Flood.

.. _json-floodmap_identifier:

* ``floodmap_identifier`` (String, optional): A string that will be appended to the
  flood map filenames to help identify the flood maps created by this NenCarta
  simulation. Default is an empty string.

.. _json-floodmap_mode:

* ``floodmap_mode`` (Bool, optional): Either "forecast" or "user". Forecast mode
  will run either a GEOGLOWS or NWM forecast flows. User mode allows the option
  ``user_flow_files`` to be populated and used instead. Default "forecast".

.. _json-flowline:

* ``flowline`` (String): A full filepath to the flowline shapefile that you'll be
  using to run NenCarta.

.. _json-forensic_forecast_date:

* ``forensic_forecast_date`` (String, optional): If you want to use a past forecast,
  you can specify the date here in YYYYMMDD format (e.g., "20250807" is August 7,
  2025) in UTC. The archive of GEOGLOWS forecasts goes back to July 1, 2024. We are
  unsure of the current limitations for the National Water Model archive of forecasts.

.. _json-forensic_forecast_hour:

* ``forensic_forecast_hour`` (String, optional): If you want to use a past National
  Water Model forecast, along with the forensic_forecast_date, you must also specify a
  forensic forecast hour. This is the hour the forecast was produced, in UTC. For the
  "NWM_short_range" forecast, the forensic forecast hour must be between 0 and 23,
  expressed as a string (e.g., "00"). For the "NWM_medium_range" forecast, the
  forensic forecast hour must be "00", "06", "12", or "18". For the
  "NWM_long_range" forecast, the forensic forecast hour must be "00".

.. _json-geoglows_vpu:

* ``geoglows_vpu`` (Integer, optional): Only used for CONUS Vector Processing Units
  (VPUs) in GEOGLOWS. When the NenCarta user wants to use the GEOGLOWS map-tables to
  identify where flooding is forecast in CONUS, the GEOGLOWS VPU can be specified in
  this option. NenCarta only requires that the streamline vector for the VPU be
  available in this use case. NenCarta will use the location of flowlines forecasts to
  meet or exceed the 2-year discharge to both download USGS 3DEP DEM data and simulate
  flood inundation.

.. _json-lake_filter_json:

* ``lake_filter_json`` (String, optional): The path to the GEOGLOWS json that
  describes which stream reaches are within a lake. Optional input. This is currently
  only functional for GEOGLOWS data.

.. _json-land_watervalue:

* ``land_watervalue`` (Int, optional): The value in the land cover raster that
  represents water. Default 80.

.. _json-make_ap_database:

* ``make_ap_database`` (Bool, optional): Whether ARC will make an Area-Perimeter
  file. Default True.

.. _json-make_curvefile:

* ``make_curvefile`` (Bool, optional): Whether ARC will make a Curve file. Default
  True.

.. _json-make_depth_maps:

* ``make_depth_maps`` (Bool, optional): Whether or not to make flood depth maps.
  Default True.

.. _json-make_fist_inputs:

* ``make_fist_inputs`` (Bool, optional): Whether or not to make FIST inputs for
  flood. Default True.

.. _json-make_velocity_maps:

* ``make_velocity_maps`` (Bool, optional): Whether or not to make flood velocity
  maps. Default True.

.. _json-make_wse_maps:

* ``make_wse_maps`` (Bool, optional): Whether or not to make flood water surface
  elevation maps. Default True.

.. _json-mannings_text_file:

* ``mannings_text_file`` (String, optional): The full filepath to the Manning's n
  text file to be used in the flood mapping process. If not provided, NenCarta will
  use its built-in default Manning's n values.

.. _json-mapper:

* ``mapper`` (String, optional): Here you're specifying if you're running
  "FloodSpreader", "Curve2Flood-Kernel Weighted", or "Curve2Flood-FLDPLNpy", or
  "Curve2Flood-Multi-Point Interpolation" when performing bathymetry estimation and
  flood inundation mapping. Defaults to FloodSpreader

.. _json-min_match_score:

* ``min_match_score`` (Float, optional): This is the threshold value that is used to
  determine if a good match is made when using the
  ``move_stream_network_to_new_locations`` option. NenCarta scores the match between
  each terrain-derived stream segment and nearby segments from the original stream
  network using buffered corridor overlap. The best candidate is retained for each
  new segment and any match with a score below ``min_match_score`` is discarded.

.. _json-move_stream_network_to_new_locations:

* ``move_stream_network_to_new_locations`` (Bool, optional): If True, this option
  directs NenCarta to build a terrain-derived stream network from the DEM, delineate
  threshold-based catchments, and then transfer stream IDs from the original
  flowline dataset onto the new network. Default is False. This is required if using
  FLDPLNpy as the ``mapper`` option. This option should not be used if your DEM does
  not contain the entire contribution upstream area of your area of interest. When
  FLDPLNpy is selected and the DEM CRS is geographic, NenCarta automatically
  reprojects the DEM to ``WGS 84 / NSIDC EASE-Grid 2.0 Global`` (EPSG:6933)
  before this hydroterrain workflow begins.

.. _json-name:

* ``name`` (String): The name of the watershed you're modeling.

.. _json-new_strm_threshold_km2:

* ``new_strm_threshold_km2`` (Float, optional): This value represents the
  terrain threshold used when NenCarta extracts a new stream mask, vector flowlines,
  and catchments from the DEM-derived flow direction and accumulation rasters. The
  value is in square km and is required when
  ``move_stream_network_to_new_locations`` is True.

.. _json-nwm_api_key:

* ``nwm_api_key`` (String, optional): Required when streamflow_source is set to "NWM"
  or any "NWM_*" option. This is the NWM API key passed as the 'x-api-key' header for
  NWM requests. You must apply for an API key using `these instructions 
  <https://docs.ciroh.org/docuhub-staging/docs/products/data-management/bigquery-api/>`_.

.. _json-output_dir:

* ``output_dir`` (String): The full filepath to the directory where your output will
  be saved.

.. _json-overwrite_floodmaps:

* ``overwrite_floodmaps`` (Bool, optional): Whether to replace existing flood
  indunation, depth, WSE, etc. maps if they exist. Defaults to True.

.. _json-process_stream_network:

* ``process_stream_network`` (Bool, optional): Setting this value to True will direct
  the forecast system to take the flowline network (your flowline variable) and
  determine which flowlines are within each of your DEM tiles and download the
  necessary ECMWF GEOGLOWS Streamflow Service historic data for each DEM tile. Setting
  this value to False will bypass the creation of these system inputs and assumes that
  those inputs already exist. Default False.

.. _json-q_baseflow_threshold:

* ``q_baseflow_threshold`` (Float, optional): Setting this value (in cubic meters per
  second) equal to a float value will filter out all streams in your domain that have
  a baseflow (from ``specified_bathyflow_field``) that are less than the specified
  value. Default is None.

.. _json-quiet:

* ``quiet`` (Bool, optional): Setting this value to True will suppress ARC and
  Curve2Flood output. Default False.

.. _json-remove_old_forecast_files:

* ``remove_old_forecast_files`` (Bool, optional): If True, check existing flood maps
  and see if they have forecast data older than ``age_of_forecast_days``. If so,
  remove. Default True.

.. _json-specified_bathyflow_field:

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

.. _json-specify_depths_for_bathy_mask:

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

.. _json-specified_highflow_field:

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

.. _json-streamflow_source:

* ``streamflow_source`` (String, optional): Setting this equal to "GEOGLOWS" will
  force NenCarta to use GEOGLOWS retrospective and forecast streamflow data. The
  deafult is GEOGLOWS. Setting this to "NWM_short_range" will force NenCarta to use
  the National Water Model retrospective and short-range forecast streamflow data.
  Setting this to "NWM_medium_range" will force NenCarta to use the National Water
  Model retrospective and medium-range forecast streamflow data. Setting this to
  "NWM_long_range" will force NenCarta to use the National Water Model retrospective
  and long-range forecast streamflow data. Default "GEOGLOWS".

.. _json-StrmOrder_Field:

* ``StrmOrder_Field`` (String, optional): The field in the flowline GIS data that
  specifies the stream order of the streams in your model domain. This input is
  required if you plan to use StrmOrder_Lower or StrmOrder_Upper to limit which
  streams will be used for flood inundation mapping by NenCarta.

.. _json-StrmOrder_Lower:

* ``StrmOrder_Lower`` (Integer, optional): The lowest value of stream order that you
  plan to use in your NenCarta simulation.

.. _json-StrmOrder_Upper:

* ``StrmOrder_Upper`` (Integer, optional): The highest value of stream order that you
  plan to use in your NenCarta simulation.

.. _json-use_specified_depth_for_bathy_mask:

* ``use_specified_depth_for_bathy_mask`` (Bool, optional): Setting this value to
  False will direct NenCarta to create a water mask that is a compilation of the
  stream network raster and the land use designated as water. This will be used to
  clean the input DEM and burn bathymetry into the DEM. Setting this value to True
  will direct NenCarta to create a water mask for DEM cleaning that fills the stream
  cell with a specified depth of water. The depth of water will be based upon the
  value(s) specified in the ``specify_depths_for_bathy_mask`` argument. Default True.

.. _json-use_warning_flags_to_download_dem:

* ``use_warning_flags_to_download_dem`` (Bool, optional): TODO. Default False.

.. _json-user_flow_files:

* ``user_flow_files`` (String or List of Strings, optional): If ``floodmap_mode`` is
  "user", than use this file or list of files to create flood maps instead of looking
  at the forecast.

.. _json-vdt_file_extension:

* ``vdt_file_extension`` (String, optional): The file extension of the VDT files to
  be created. Default "txt".

Required watershed keys
-----------------------

Each watershed definition requires these arguments in the JSON file. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* :ref:`name <json-name>`
* :ref:`flowline <json-flowline>`
* :ref:`dem_dir <json-dem_dir>`
* :ref:`output_dir <json-output_dir>`

Common processing options
-------------------------

These options control the main flood-mapping workflow. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* :ref:`process_stream_network <json-process_stream_network>`
* :ref:`clean_dem <json-clean_dem>`
* :ref:`use_warning_flags_to_download_dem <json-use_warning_flags_to_download_dem>`

Forecast and flow options
-------------------------

These options select the streamflow source and forecast behavior. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* :ref:`streamflow_source <json-streamflow_source>`: Forecast provider and product selection.
* :ref:`forensic_forecast_date <json-forensic_forecast_date>`: Use a prior forecast by date in ``YYYYMMDD`` format.
* :ref:`forensic_forecast_hour <json-forensic_forecast_hour>`: Required for archived NWM forecasts.
* :ref:`age_of_forecast_days <json-age_of_forecast_days>`: Remove outdated forecast outputs based on age.
* :ref:`remove_old_forecast_files <json-remove_old_forecast_files>`: Enable cleanup of stale forecast products.
* :ref:`user_flow_files <json-user_flow_files>`: Provide user-supplied flow files when :ref:`floodmap_mode <json-floodmap_mode>` is ``user``.

ARC options
-----------
These options control the ARC workflow that estimates bathymetry and creates curve files. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* :ref:`create_reach_average_curve_file <json-create_reach_average_curve_file>`
* :ref:`make_curvefile <json-make_curvefile>`
* :ref:`make_ap_database <json-make_ap_database>`
* :ref:`bathy_args <json-bathy_args>`
* :ref:`vdt_file_extension <json-vdt_file_extension>`

ARC outputs
~~~~~~~~~~~

For each DEM tile, NenCarta writes bathymetry-related outputs under
``output_dir`` / ``name``: 

* ``VDT/<NWM|GEOGLOWS>_<DEM>_VDT_Database_Bathy.<vdt_file_extension>``: Bathymetry
  VDT database.
* ``VDT/<NWM|GEOGLOWS>_<DEM>_VDT_FS_Bathy.csv``: Bathymetry VDT test file.
* ``VDT/<NWM|GEOGLOWS>_<DEM>_CurveFile_Bathy.csv``: Bathymetry curve file, written
  when ``make_curvefile`` is ``true``.
* ``VDT/<NWM|GEOGLOWS>_<DEM>_AP_Database_Bathy.txt``: Area-perimeter database,
  written when ``make_ap_database`` is ``true``.


Bathymetry options
------------------

These options control bathymetry estimation generation. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* :ref:`disable_bathymetry <json-disable_bathymetry>`
* :ref:`bathy_use_banks <json-bathy_use_banks>`
* :ref:`use_specified_depth_for_bathy_mask <json-use_specified_depth_for_bathy_mask>`
* :ref:`specify_depths_for_bathy_mask <json-specify_depths_for_bathy_mask>`
* :ref:`find_banks_based_on_landcover <json-find_banks_based_on_landcover>`

Bathymetry outputs
~~~~~~~~~~~~~~~~~~

For each DEM tile, NenCarta writes bathymetry-related outputs under
``output_dir`` / ``name``. ARC and the flood mapper (e.g., Curve2Flood) 
create the main bathymetry products:

* ``Bathymetry/<NWM|GEOGLOWS>_<DEM>_ARC_Bathy.tif``: ARC bathymetry raster written
  from the ARC bathymetry run.
* ``Bathymetry/<NWM|GEOGLOWS>_<DEM>_FS_Bathy.tif``: FloodSpreader or Curve2Flood
  bathymetry raster used later as the DEM input for flood mapping when bathymetry is
  enabled.
* ``FloodMap/<NWM|GEOGLOWS>_<DEM>_ARC_Flood[_floodmap_identifier]_Bathy.tif``:
  Bathymetry flood raster generated during the mapper bathymetry stage.
* ``FloodMap/<NWM|GEOGLOWS>_<DEM>_ARC_Flood[_floodmap_identifier]_Bathy.shp``:
  Bathymetry flood geometry written alongside the bathymetry flood raster.

If :ref:`disable_bathymetry <json-disable_bathymetry>` is ``true``, the code still writes the hydraulic support
files in ``VDT/`` needed for later flood mapping, but it returns before creating the
``ARC_Bathy.tif``, ``FS_Bathy.tif``, and bathymetry flood-map outputs.


Flood-map options
-----------------
These options control the flood-map generation process. They are defined in 
`this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_.

* :ref:`mapper <json-mapper>`
* :ref:`make_depth_maps <json-make_depth_maps>`
* :ref:`make_velocity_maps <json-make_velocity_maps>`
* :ref:`make_wse_maps <json-make_wse_maps>`
* :ref:`floodmap_identifier <json-floodmap_identifier>`
* :ref:`floodmap_args <json-floodmap_args>`

Flood-map outputs
~~~~~~~~~~~~~~~~~

Flood-map outputs are written under ``output_dir`` / ``name`` / ``FloodMap``. The
exact filenames vary depending on whether you are using forecast flows or
:ref:`user_flow_files <json-user_flow_files>`:

* Flood extent raster: NenCarta always writes a flood extent raster through
  ``OutFLD``. For forecast mode this becomes a forecast-named file such as
  ``<NWM|GEOGLOWS>_<DEM>_ARC_Flood[_floodmap_identifier]_Forecast_<date>[ _<hour>].tif``.
  For user-flow mode the flow-file stem is appended to the flood-map filename.
* Flood geometry: NenCarta writes a companion geometry file through ``OutSHP`` when
  :ref:`floodmap_args <json-floodmap_args>` does not set ``Make_Output_GPKG`` to
  ``False``. The path uses the
  same base name as the flood extent raster and changes the suffix to ``.shp``.
* Flood depth raster: ``<streamflow_source>_<DEM>_ARC_FloodDepth[_floodmap_identifier]...``
  is written when :ref:`make_depth_maps <json-make_depth_maps>` is ``true``.
* Flood water-surface-elevation raster:
  ``<streamflow_source>_<DEM>_ARC_FloodWSE[_floodmap_identifier]...`` is written when
  :ref:`make_wse_maps <json-make_wse_maps>` is ``true``.
* Flood velocity raster:
  ``<streamflow_source>_<DEM>_ARC_FloodVEL[_floodmap_identifier]...`` is written when
  :ref:`make_velocity_maps <json-make_velocity_maps>` is ``true``.

The flood mapper uses ``FS_Bathy.tif`` as the DEM input when bathymetry is enabled.
If bathymetry is disabled, it falls back to the cleaned DEM when
:ref:`clean_dem <json-clean_dem>` is ``true`` or the original DEM otherwise.

In forecast mode, NenCarta produces one set of flood outputs per DEM tile for the
selected forecast. In user-flow mode, NenCarta produces one set of flood outputs per
input flow file.

Consequence estimation
----------------------

Set :ref:`estimate_consequences <json-estimate_consequences>` to ``true`` to invoke the Go-Consequences workflow after flood-map generation.
See `this section <https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs>`_ for more information on 
what ``estimate_consequences`` does.

Consequence estimation outputs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When :ref:`estimate_consequences <json-estimate_consequences>` is ``true``, ``nencarta/main.py`` iterates over the
depth rasters returned by the flood-mapping stage and creates one consequences run per
depth raster. The outputs are:

* ``FloodMap/<depth_file_stem>_WGS84.tif``: A WGS84 copy of the depth raster created
  with ``gdal.Warp`` before the Docker run. If reprojection fails, the original depth
  raster is used instead.
* ``Consequences/<depth_file_stem>_WGS84_consequences.json`` or
  ``Consequences/<depth_file_stem>_consequences.json``: The Go-Consequences config
  JSON written by ``Create_Go_Consequence_GeoJSON``.
* ``Consequences/<depth_file_stem>_WGS84_consequences.gpkg`` or
  ``Consequences/<depth_file_stem>_consequences.gpkg``: The GeoPackage results file
  written by the Go-Consequences Docker container.

The consequences JSON points Go-Consequences to the flood-depth raster in
``/data/FloodMap`` and writes the results GeoPackage to ``/data/Consequences`` inside
the container, which corresponds to the watershed output directory on the host.

If :ref:`estimate_consequences <json-estimate_consequences>` is ``true`` and
:ref:`make_depth_maps <json-make_depth_maps>` is ``false``, the code forces
:ref:`make_depth_maps <json-make_depth_maps>` back to ``true`` before processing so
that a depth
raster exists for the consequences workflow.

FIST options
------------

NenCarta can create inputs for the Flood Inundation Surface Topology (FIST) model as a forecast. To do this, set
``make_fist_inputs`` to ``true`` to generate FIST-ready inputs as part of the workflow.
NenCarta writes those files to the ``FIST`` subdirectory in the
``output_dir``/ ``name`` directory.

NenCarta relies on ARC to generate the FIST inputs. For the underlying ARC workflow, see
the `ARC FIST documentation <https://automated-rating-curve.readthedocs.io/en/latest/making_inputs_for_fist/>`_.


FIST outputs
~~~~~~~~~~~~

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

Stream network movement options
-------------------------------

* :ref:`move_stream_network_to_new_locations <json-move_stream_network_to_new_locations>`
* :ref:`new_strm_threshold_km2 <json-new_strm_threshold_km2>`
* :ref:`min_match_score <json-min_match_score>`

When ``move_stream_network_to_new_locations`` is enabled, or when
``mapper`` is set to ``Curve2Flood-FLDPLNpy``, NenCarta switches to the stream
movement workflow implemented in ``nencarta/nencarta/main.py``. That workflow
first checks the DEM coordinate system. If the DEM is geographic
(``lat/lon``), NenCarta creates a projected GeoTIFF copy in
``WGS 84 / NSIDC EASE-Grid 2.0 Global`` (EPSG:6933) and uses that projected
copy for stream preprocessing, hydroterrain generation, DEM cleaning,
bathymetry, flood mapping, and FIST output generation for the rest of that DEM
tile's run. The hydroterrain workflow
first calls ``create_flow_direction_and_flow_accumulation_raster`` in
``Hydroterrain_Processing.py`` to fill depressions in the DEM and create a
filled DEM, D8 flow-direction raster, and D8 flow-accumulation raster. 
All hydroterrain processing is conducted using 
`WhiteboxTools <https://github.com/jblindsay/whitebox-tools>`_.

NenCarta then calls
``create_catchments_and_flowlines_with_flow_direction_and_accumulation`` to
extract a thresholded stream raster from the accumulation grid, convert that
raster to vector flowlines, and delineate threshold-based catchments. By
default the function keeps the vectorized stream reaches intact and assigns
each reach a ``catchment_id`` from a point sampled just upstream of the
reach's downstream endpoint. This avoids the small sliver segments that can
appear when vector flowlines are split at catchment boundaries. The
terrain-derived network therefore carries fields such as ``catchment_id``,
``stream_id``, ``downstream_id``, and ``upstream_ids``.

Direct callers can still request the legacy overlay behavior by passing
``catchment_assignment_mode="intersection"`` to
``create_catchments_and_flowlines_with_flow_direction_and_accumulation``. That
mode intersects vectorized streams with catchment polygons before building
topology, which can be useful for reproducing older outputs but is more likely
to create short stream segments near junctions.

The final step is ``match_new_streams_to_old_streams``. That method compares the
terrain-derived flowlines to the originally processed stream network, ranks
candidate matches by buffered overlap, transfers the original stream IDs
(``LINKNO``/``DSLINKNO`` for GEOGLOWS or ``COMID``/``TOCOMID`` for NWM), copies
stream order when available, and removes low-scoring or detached subnetworks.
The matched network becomes the stream layer used by the rest of the NenCarta
workflow, and the filled DEM replaces the original DEM for downstream steps.

If ``process_stream_network`` is ``false`` and the moved stream network products
already exist, NenCarta reuses the existing matched flowlines and filled DEM
instead of rebuilding them.


Stream network movement outputs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This workflow writes its outputs beneath the watershed's ``FlowDirection`` and
``STRM`` subdirectories:

* ``FlowDirection/{DEM}.tif``: projected DEM copy created only when
  ``Curve2Flood-FLDPLNpy`` is used with a geographic DEM. The output is written
  as a GeoTIFF in ``WGS 84 / NSIDC EASE-Grid 2.0 Global`` (EPSG:6933) and
  becomes the DEM used by subsequent NenCarta steps for that tile.

* ``FlowDirection/{DEM}_filled.tif``: depression-filled DEM created before
  stream extraction. When stream movement is active, this becomes the DEM used
  by downstream processing.

* ``FlowDirection/{DEM}_flowdir.tif``: D8 flow-direction raster derived from the
  filled DEM.

* ``FlowDirection/{DEM}_flowacc.tif``: D8 flow-accumulation raster derived from
  the filled DEM.

* ``FlowDirection/{DEM}_flowlines.gpkg``: terrain-derived flowline network
  created from the thresholded stream raster. Its ``flowlines`` layer includes
  topology fields such as ``catchment_id``, ``stream_id``, ``id``,
  ``downstream_id``, and ``upstream_ids``.

* ``FlowDirection/{DEM}_catchments.gpkg``: catchment polygons generated from the
  thresholded stream mask. The ``catchments`` layer stores ``catchment_id`` for
  each delineated polygon.

* ``STRM/{DEM}_flowlines_matched.gpkg``: matched flowline network created by
  transferring IDs from the original stream dataset onto the terrain-derived
  flowlines. This file is the stream network used for later NenCarta steps.
  Along with the transferred source-network IDs and optional stream order, it
  also stores match diagnostics including ``match_score``, ``centroid_dist_m``,
  ``line_dist_m``, ``overlap_area_m2``, ``overlap_ratio``, and
  ``overlap_hit``.

During stream extraction NenCarta also creates intermediate threshold rasters
and shapefiles inside ``FlowDirection`` (for example the thresholded stream
mask and subbasin raster). Those files support the build process, while the
GeoPackages above are the persistent vector outputs.


GUI options
-----------

When you load the GUI, the number of inputs can be confusing. Below we describe how each input in the GUI corresponds to the JSON inputs 
described above. The GUI will create a JSON file and then execute the NenCarta workflow. The GUI outputs the JSON file in the 
``output_dir`` directory with the filename ``{name}.json``.The GUI inputs are organized into sections that correspond 
to the categories of JSON inputs described above.

The GUI writes a JSON file with top-level ``parallel`` set to ``False`` and
``num_workers`` set to ``1``. Those two top-level values are currently not
user-configurable in the GUI.

Required Inputs
~~~~~~~~~~~~~~~

* ``Watershed Name`` -> ``name``
* ``Flowline File`` -> ``flowline``
* ``DEM Directory`` -> ``dem_dir``
* ``Output Directory`` -> ``output_dir``

Key Workflow Switches
~~~~~~~~~~~~~~~~~~~~~

* ``Clean DEM (Requires Initial Flood Map Step)`` -> ``clean_dem``
* ``Estimate Consequences (Run Go-Consequences)`` -> ``estimate_consequences``
* ``Mapper Method`` -> ``mapper``
* ``Streamflow Source`` -> ``streamflow_source``
* ``NWM API Key`` -> ``nwm_api_key``

For ``Streamflow Source``, the GUI labels map to the JSON values as follows:

* ``GEOGLOWS`` -> ``GEOGLOWS``
* ``NWM Short Range`` -> ``NWM_short_range``
* ``NWM Medium Range`` -> ``NWM_medium_range``
* ``NWM Long Range`` -> ``NWM_long_range``

Advanced Parameters
~~~~~~~~~~~~~~~~~~~

* ``Disable Bathymetry`` -> ``disable_bathymetry``
* ``Bathy Use Banks`` -> ``bathy_use_banks``
* ``Flood LC and Stream Cells in Flood Map`` -> ``flood_waterlc_and_strm_cells``
* ``Use Specified Depth for Bathy Mask`` -> ``use_specified_depth_for_bathy_mask``
* ``Find Banks Based on Land Cover (Default=True)`` -> ``find_banks_based_on_landcover``
* ``Process Stream Network`` -> ``process_stream_network``
* ``Create Reach Average Curve File`` -> ``create_reach_average_curve_file``
* ``Use Warning Flags to Download DEM`` -> ``use_warning_flags_to_download_dem``
* ``Land Water Value`` -> ``land_watervalue``
* ``Age of Forecast Days`` -> ``age_of_forecast_days``
* ``Specific flood depths (in meters) for bathy mask`` -> ``specify_depths_for_bathy_mask``
* ``GEOGLOWS VPU ID`` -> ``geoglows_vpu``
* ``Forensic Forecast Date`` -> ``forensic_forecast_date``
* ``Forensic Forecast Hour`` -> ``forensic_forecast_hour``
* ``Bathy Flow Field`` -> ``specified_bathyflow_field``
* ``High Flow Field`` -> ``specified_highflow_field``
* ``Move Stream Network to Match DEM (Optional)`` -> ``move_stream_network_to_new_locations``
* ``Stream Threshold for New Stream Network`` -> ``new_strm_threshold_km2``
* ``Minimum Match Score`` -> ``min_match_score``
* ``Stream Order Field`` -> ``StrmOrder_Field``
* ``Stream Order Lower`` -> ``StrmOrder_Lower``
* ``Stream Order Upper`` -> ``StrmOrder_Upper``
* ``Baseflow Threshold`` -> ``q_baseflow_threshold``
* ``Lake Filter JSON`` -> ``lake_filter_json``
* ``Overwrite Forecast Floodmaps`` -> ``overwrite_floodmaps`` conceptually, but the
  current GUI field key is ``overwrite_forecast_floodmaps``
* ``Remove Old Forecast Files`` -> ``remove_old_forecast_files``
* ``Make FIST Inputs`` -> ``make_fist_inputs``
* ``DEM Filter`` -> ``dem_filter``
* ``Floodmap Mode`` -> ``floodmap_mode``
* ``User Flow Files (one per line)`` -> ``user_flow_files``
* ``Make Curve Files`` -> ``make_curvefile`` conceptually, but the current GUI field
  key is ``make_curvefiles``
* ``Make Area-Perimeter Database`` -> ``make_ap_database``
* ``Make Depth Maps`` -> ``make_depth_maps``
* ``Make Velocity Maps`` -> ``make_velocity_maps``
* ``Make WSE Maps`` -> ``make_wse_maps``
* ``VDT File Extension`` -> ``vdt_file_extension``
* ``Manning's n Text File`` -> ``mannings_text_file``
* ``Pre-processsing/Bathymetry Arguments`` -> ``bathy_args``
* ``Floodmap Arguments`` -> ``floodmap_args``

GUI-specific conversions
~~~~~~~~~~~~~~~~~~~~~~~~

* The GUI stores ``Watershed Name`` as ``watershed_name`` internally and then writes
  it to the watershed JSON as ``name``.
* ``Specific flood depths (in meters) for bathy mask`` is entered as comma-separated
  text in the GUI and written as a JSON list for
  ``specify_depths_for_bathy_mask``.
* ``User Flow Files (one per line)`` is entered as multi-line text in the GUI and
  written as a JSON list for ``user_flow_files``.
* ``Stream Order Field``, ``Stream Order Lower``, and ``Stream Order Upper`` are
  stored internally as ``strmorder_field``, ``strmorder_lower``, and
  ``strmorder_upper`` before being mapped to ``StrmOrder_Field``,
  ``StrmOrder_Lower``, and ``StrmOrder_Upper`` in the JSON.
