Usage
=====

NenCarta supports three primary execution modes:

* GUI mode
* JSON-driven runs
* Direct CLI arguments

GUI mode
--------

Launch the built-in graphical interface with:

.. code-block:: bash

   flood-mapping gui

JSON mode
---------

Use the ``json`` subcommand when you want to run one or more watersheds from a structured configuration file.

Example:

.. code-block:: json

   {
     "watersheds": [
       {
         "name": "yellowstone_example",
         "flowline": "C:/path/to/flowline.shp",
         "dem_dir": "C:/path/to/dem_dir",
         "output_dir": "C:/path/to/output",
         "process_stream_network": true,
         "mapper": "FloodSpreader",
         "streamflow_source": "NWM_short_range",
         "nwm_api_key": "YOUR_NWM_API_KEY"
       }
     ]
   }

Run the file in serial mode:

.. code-block:: bash

   flood-mapping json "/path/to/your.json" --serial

Run the file in parallel mode:

.. code-block:: bash

   flood-mapping json "/path/to/your.json" --parallel --num_workers 8

CLI mode
--------

Use the ``cli`` subcommand to run a single watershed directly from the terminal:

.. code-block:: bash

   flood-mapping cli ExampleWatershed "C:\path\to\flowline.shp" "C:\path\to\dem_dir" "C:\path\to\output" --process_stream_network --mapper FloodSpreader --streamflow_source NWM_short_range --nwm_api_key "YOUR_NWM_API_KEY"

Forecast sources
----------------

NenCarta can use:

* ``GEOGLOWS``
* ``NWM_short_range``
* ``NWM_medium_range``
* ``NWM_long_range``

If you select an NWM source, you must supply ``nwm_api_key``.
