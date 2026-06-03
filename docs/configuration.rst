Configuration Reference
=======================

Required watershed keys
-----------------------

Each watershed definition requires these fields:

* ``name``
* ``flowline``
* ``dem_dir``
* ``output_dir``

Common processing options
-------------------------

These options control the main flood-mapping workflow:

* ``process_stream_network``: Build stream-network inputs for each DEM tile.
* ``mapper``: Choose ``FloodSpreader`` or one of the Curve2Flood modes.
* ``clean_dem``: Enable DEM cleaning before the core workflow.
* ``disable_bathymetry``: Skip ARC and bathymetry-based topobathymetric processing.
* ``move_stream_network_to_new_locations``: Recreate and match a DEM-derived stream network.
* ``new_strm_threshold_km2``: Required when moving the stream network or using ``Curve2Flood-FLDPLNpy``.

Forecast and flow options
-------------------------

These options select the streamflow source and forecast behavior:

* ``streamflow_source``: Forecast provider and product selection.
* ``forensic_forecast_date``: Use a prior forecast by date in ``YYYYMMDD`` format.
* ``forensic_forecast_hour``: Required for archived NWM forecasts.
* ``age_of_forecast_days``: Remove outdated forecast outputs based on age.
* ``remove_old_forecast_files``: Enable cleanup of stale forecast products.
* ``user_flow_files``: Provide user-supplied flow files when ``floodmap_mode`` is ``user``.

Bathymetry and flood-map options
--------------------------------

These options control bathymetry estimation and flood-map generation:

* ``bathy_use_banks``
* ``use_specified_depth_for_bathy_mask``
* ``specify_depths_for_bathy_mask``
* ``find_banks_based_on_landcover``
* ``create_reach_average_curve_file``
* ``make_curvefile``
* ``make_ap_database``
* ``make_depth_maps``
* ``make_velocity_maps``
* ``make_wse_maps``
* ``floodmap_identifier``
* ``floodmap_args``
* ``bathy_args``

Consequence estimation
----------------------

Set ``estimate_consequences`` to ``true`` to invoke the Go-Consequences workflow after flood-map generation.

Input examples
--------------

See the repository ``README.md`` for a larger end-to-end JSON example and a fuller field-by-field discussion.
