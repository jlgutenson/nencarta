# About NenCarta
NenCarta is a workflow designed to automate the creation of riverine flood inundation maps using the Automated Rating Curve (ARC) tool and Curve2Flood and streamflow from either the National Water Model or the GEOGLOWS ECWMF Streamflow Service. For the coterminous United States, NenCarta can then take the resulting flood inundaton maps and estimate direct flood consequences using Go-Consequences and the National Structure Inventory (NSI).

Here is an overview of how NenCarta produces flood inundation maps and performs consequence assessment.

[![NenCarta Overview](https://github.com/jlgutenson/nencarta/blob/main/workflow.png)](https://github.com/jlgutenson/nencarta/blob/main/workflow.png)

# Setting up NenCarta

Below are instructions for setting up NenCarta using Miniconda and Docker.

1. Clone this repository and return to the command prompt window and navigate to where the NenCarta repository was cloned locally.

2. Create the necessary conda environment with the following command:
```bash
conda env create -f environment.yaml
```

3. Activate the conda environment:
```bash
conda activate nencarta_py310
```

4. Install the Automated Rating Curve (ARC) by cloning the repository and installing using the instructions provided [in ARC's README](https://github.com/MikeFHS/automated-rating-curve).

5. Install the Curve2Flood by cloning the repository and installing using the instructions provided in [Curve2Flood's README](https://github.com/MikeFHS/curve2flood).

6. Inside the NenCarta repository run:
```bash
pip install .
```

7. Optional, if you want to compute economic consequences using the resulting flood inundation map, install Go-Consequences creating a Go-Consequences Docker container using the Dockerfile found [in this repository](https://github.com/jlgutenson/nencarta/blob/main/Dockerfile.prod). The easiet way to do this is to install Docker (e.g., [Docker Desktop](https://www.docker.com/products/docker-desktop/)), navigate to where the NenCarta repository was cloned locally using the command prompt window, and running the command:

```bash
docker build --progress=plain --file Dockerfile.prod -t go-consequences:latest .
```

8. You should be set up now. You can run `flood-mapping -h` in your command prompt to see if things installed properly. Have fun you dirty animals! 

# How to Run NenCarta

There are three ways to run NenCarta, either by providing a JSON input's filepath, command line arguments, or through a built-in graphical user interface (GUI).

## JSON File Arguments
The JSON should look like what's provided below. Multiple watersheds can be provided in this JSON format

```
{
    "watersheds": [
        {
            "name": "yellowstone_wsebathy_notclean_2_nencartatest",
            "flowline": "C:/Users/follumm/Desktop/CHL_MultiModel/Yellowstone_2022_Flood/StrmShp/Yellowstone_flood_2022_stream_reaches_for_ARC.shp",
            "dem_dir": "C:/Users/follumm/Desktop/CHL_MultiModel/Yellowstone_2022_Flood/DEM_FABDEM_merged",
            "bathy_use_banks": false,
            "clean_dem": true,
            "mapper": "FloodSpreader",
            "output_dir": "C:/Users/follumm/Desktop/CHL_MultiModel/Yellowstone_2022_Flood/Results",
            "process_stream_network": true,
            "use_specified_depth_for_bathy_mask": true,
            "age_of_forecast_days": 7,
            "find_banks_based_on_landcover": true,
            "specify_depths_for_bathy_mask": [1.0, 2.0],
            "create_reach_average_curve_file": false,
            "forensic_forecast_date":"20250807",
            "specified_bathyflow_field":"qout_median",
            "specified_highflow_field":"rp100_premium",
            "lake_filter_json":"C:/Users/follumm/Desktop/CHL_MultiModel/Yellowstone_2022_Flood/StrmShp/Yellowstone_flood_2022_stream_reaches_for_ARC_that_are_in_lakes.json",
            "streamflow_source": "NWM",
            "geoglows_vpu":715,
            "nwm_api_key": "YOUR_NWM_API_KEY"
        }
    ]
}
```

These inputs are:

* **`age_of_forecast_days`** (Integer, optional): This is how old previous forecasts will be allowed to be based upon the current day. For example, specifying 7 days will delete all forecast flood inundation maps that are older than 7 days. Default 7.

* **`bathy_args`** (Dictionary, optional): A dictionary of arguments that will be passed to ARC and the flood mapper when estimating bathymetry in each cross-section. See the ARC and the respective flood mapper documentation for more information on these arguments.

* **`bathy_use_banks`** (Bool, optional): Setting this to True will allow the system to use estimated bank elevations to estimate bathymetry using the streamflow specified in specified_bathyflow_field. Setting this value to False will allow the system to use the estimated water surface elevation to estimate bathymetry the using the streamflow specified in specified_bathyflow_field. Default False.

* **`clean_dem`** (Bool, optional): Setting this value to True will allow the system to use the DEM Cleaner. Setting this value to False will bypass use of the DEM Cleaner. Default False.

* **`create_reach_average_curve_file`** (Bool, optional): Setting this value to true instructs NenCarta to direct ARC to output the curve file by taking velocity, depth, and top-width estimates for all stream cells on an individual stream reach and creating the curve parameters for each stream cell. Setting this value to False instructs NenCarta to direct ARC to output the curve file using velocity, depth, and top-width estimates for each stream cell. Default False.

* **`dem_dir`** (String): A full filepath to the directory containing one or more DEMs that you will be using as input in NenCarta. Currently, NenCarta, ARC, and Curve2Flood require that the DEM be in a geographic coordinate system like the North American Datum of 1983 (NAD83) or World Geodetic System 1984 (WGS84).

* **`dem_filter`** (String, optional): A glob string with which files in the `dem_dir` must match to be included in the run. By default, "*", or all files.

* **`estimate_consequences`** (Bool, optional): Setting this equal to true will utilize Go-Consequences and the National Structure Inventory (NSI) to perform consequence assessment for the area within each flood inundation map. This functionality is currently only available for the coterminous United States. Default False.

* **`find_banks_based_on_landcover`** (Bool, optional): Setting this value to True will direct NenCarta to first try finding the banks using the land cover. If the stream cell is in a water pixel, NenCarta will direct ARC to search for the end of the water surrounding the stream cell and this will location will be designated as the banks. Setting this value to False will direct NenCarta and ARC to find the banks by assuming the channel is flat in the DEM and allowing ARC to find the banks when the flat water surface ends in the cross-section. Default True.

* **`flood_waterlc_and_strm_cells`** (Bool optional): Argument for the flood mapper, whether to force in the flood map all cells that are wet in the land cover and stream raster as wet in the flood map. Default False.

* **`floodmap_args`** (Dictionary, optional): A dictionary of arguments that will be passed to the flood mapper when creating flood inundation maps. See the respective documentation for more information on these arguments.

* **`floodmap_identifier`** (String, optional): A string that will be appended to the flood map filenames to help identify the flood maps created by this NenCarta simulation. Default is an empty string.

* **`floodmap_mode`** (Bool, optional): Either "forecast" or "user". Forecast mode will run either a GEOGLOWS or NWM forecast flows. User mode allows the option `user_flow_files` to be populated and used instead. Default "forecast".

* **`flowline`** (String): A full filepath to the flowline shapefile that you'll be using to run NenCarta.

* **`forensic_forecast_date`** (String, optional): If you want to use a past forecast, you can specify the date here in YYYYMMDD format (e.g., "20250807" is August 7, 2025) in UTC. The archive of GEOGLOWS forecasts goes back to July 1, 2024. We are unsure of the current limitations for the National Water Model archive of forecasts.

* **`forensic_forecast_hour`** (String, optional): If you want to use a past National Water Model forecast, along with the forensic_forecast_date, you must also specify a forensic forecast hour. This is the hour the forecast was produced, in UTC. For the "NWM_short_range" forecast, the forensic forecast hour must be between 0 and 23, expressed as a string (e.g., "00"). For the "NWM_medium_range" forecast, the forensic forecast hour must be "00", "06", "12", or "18". For the "NWM_long_range" forecast, the forensic forecast hour must be "00".

* **`geoglows_vpu`** (Integer, optional): Only used for CONUS Vector Processing Units (VPUs) in GEOGLOWS. When the NenCarta user wants to use the GEOGLOWS map-tables to identify where flooding is forecast in CONUS, the GEOGLOWS VPU can be specified in this option. NenCarta only requires that the streamline vector for the VPU be available in this use case. NenCarta will use the location of flowlines forecasts to meet or exceed the 2-year discharge to both download USGS 3DEP DEM data and simulate flood inundation. 

* **`lake_filter_json`** (String, optional): The path to the GEOGLOWS json that describes which stream reaches are within a lake. Optional input. This is currently only functional for GEOGLOWS data.

* **`land_watervalue`** (Int, optional): The value in the land cover raster that represents water. Default 80.

* **`make_ap_database`** (Bool, optional): Whether ARC will make an Area-Perimeter file. Default True.

* **`make_curvefile`** (Bool, optional): Whether ARC will make a Curve file. Default True.

* **`make_depth_maps`** (Bool, optional): Whether or not to make flood depth maps. Default True.

* **`make_fist_inputs`** (Bool optional): Whether or not to make FIST inputs for flood. Default True.

* **`make_velocity_maps`** (Bool, optional): Whether or not to make flood velocity maps. Default True.

* **`make_wse_maps`** (Bool, optional): Whether or not to make flood water surface elevation maps. Default True.

* **`mannings_text_file`** (String, optional): The full filepath to the Manning's n text file to be used in the flood mapping process. If not provided, NenCarta will use its built-in default Manning's n values.

* **`mapper`** (String, optional): Here you're specifying if you're running "FloodSpreader", "Curve2Flood", or "FLDPLNpy" when performing bathymetry estimation and flood inundation mapping. Defaults to FloodSpreader

* **`min_match_score`** (Float, optional): This is the threshold value that is used to determine if a good match is made when using the `move_stream_network_to_new_locations` option. NenCarta scores the match between each new stream and old stream by creating a 50 m buffer around each old and new stream and determining what proportion of the two areas overlap. The `min_match_score` is the minimum proportion that can be considered a credible match by the system. 

* **`move_stream_network_to_new_locations`** (Bool, optional): If True, this option will allow NenCarta to use Whitebox to create a new stream network and attempt to conflate the existing network with the new terrain derived network. Default is False. This is required if using FLDPLNpy as the `mapper` option. This option should not be used if your DEM does not contain the entire contribution upstream area of your area of interest.

* **`name`** (String): The name of the watershed you're modeling.

* **`new_strm_threshold_km2`** (Float, optional): This value represents the contributing area used to create a new stream network with the DEM when enabling the `move_stream_network_to_new_locations` option as True. The value is in square km and is required when `move_stream_network_to_new_locations`is True.

* **`nwm_api_key`** (String, optional): Required when streamflow_source is set to "NWM" or any "NWM_*" option. This is the NWM API key passed as the 'x-api-key' header for NWM requests. You must apply for an API key using these instructions: https://docs.ciroh.org/docuhub-staging/docs/products/data-management/bigquery-api/ 

* **`output_dir`** (String): The full filepath to the directory where your output will be saved.

* **`overwrite_floodmaps`** (Bool, optional): Whether to replace existing flood indunation, depth, WSE, etc. maps if they exist. Defaults to True. 

* **`process_stream_network`** (Bool, optional): Setting this value to True will direct the forecast system to take the flowline network (your flowline variable) and determine which flowlines are within each of your DEM tiles and download the necessary ECMWF GEOGLOWS Streamflow Service historic data for each DEM tile. Setting this value to False will bypass the creation of these system inputs and assumes that those inputs already exist. Default False.

* **`q_baseflow_threshold`** (Float, optional): Setting this value (in cubic meters per second) equal to a float value will filter out all streams in your domain that have a baseflow (from `specified_bathyflow_field`) that are less than the specified value. Default is None. 

* **`quiet`** (Bool, optional): Setting this value to True will suppress ARC and Curve2Flood output. Default False.

* **`remove_old_forecast_files`** (Bool, optional): If True, check existing flood maps and see if they have forecast data older than `age_of_forecast_days`. If so, remove. Default True.

* **`specified_bathyflow_field`** (String, optional): The field in the GEOGLOWS downloaded reanalysis data that will be provided to ARC to estimate bathmetry in each cross-section. For "GEOGLOWS" it must be one of"p_exceed_0", "p_exceed_5", "p_exceed_10", "p_exceed_15", "p_exceed_20", "p_exceed_25", "p_exceed_30", "p_exceed_35", "p_exceed_40", "p_exceed_45", "p_exceed_50", "p_exceed_65", "p_exceed_70", "p_exceed_75", "p_exceed_80", "p_exceed_85", "p_exceed_90", "p_exceed_95", "p_exceed_100", "rp2", "rp5", "rp10", "rp25", "rp50", "rp100","p_exceed_0_premium", or "rp100_premium". For "NWM" it must be one of "rp2", "rp5", "rp10", "rp25", "rp50", "rp100", or "rp100_premium". Default is "p_exceed_50".

* **`specify_depths_for_bathy_mask`** (List of Numbers, optional): If `use_specified_depth_for_bathy_mask` is True, the user must specify at least one depth value for FloodSpreaderPy to use when creating a water mask for the DEM cleaner and the bathymetry estimation processes. If `clean_dem` is False, this argument requires only one float value and that value will be used to create the bathymetry estimation water mask. If `clean_dem` is True, you will need to specify two float values, the first value is the depth used to create the water mask for the DEM cleaner and the second value is used to create the bathymetry estimation water mask.  The json processes accepts these inputs as a Python list. The command line interface accepts these as one or two separate values (i.e., --specify_depths_for_bathy_mask 1.0 2.0).

* **`specified_highflow_field`** (String, optional): The field in the GEOGLOWS downloaded reanalysis data that will be provided to ARC as the highest flow used to estimate water surface elevation. For "GEOGLOWS" it must be one of "p_exceed_0", "p_exceed_5", "p_exceed_10", "p_exceed_15", "p_exceed_20", "p_exceed_25", "p_exceed_30", "p_exceed_35", "p_exceed_40", "p_exceed_45", "p_exceed_50", "p_exceed_65", "p_exceed_70", "p_exceed_75", "p_exceed_80", "p_exceed_85", "p_exceed_90", "p_exceed_95", "p_exceed_100", "rp2", "rp5", "rp10", "rp25", "rp50", "rp100","p_exceed_0_premium", or "rp100_premium". For "NWM" it must be one of "rp2", "rp5", "rp10", "rp25", "rp50", "rp100", or "rp100_premium". Default is "rp100_premium".

* **`streamflow_source`** (String, optional): Setting this equal to "GEOGLOWS" will force NenCarta to use GEOGLOWS retrospective and forecast streamflow data. The deafult is GEOGLOWS. Setting this to "NWM_short_range" will force NenCarta to use the National Water Model retrospective and short-range forecast streamflow data. Setting this to "NWM_medium_range" will force NenCarta to use the National Water Model retrospective and medium-range forecast streamflow data. Setting this to "NWM_long_range" will force NenCarta to use the National Water Model retrospective and long-range forecast streamflow data. Default "GEOGLOWS".

* **`StrmOrder_Lower`** (Integer, optional): The lowest value of stream order that you plan to use in your NenCarta simulation.

* **`StrmOrder_Upper`** (Integer, optional): The highest value of stream order that you plan to use in your NenCarta simulation.

* **`use_specified_depth_for_bathy_mask`** (Bool, optional): Setting this value to False will direct NenCarta to create a water mask that is a compilation of the stream network raster and the land use designated as water. This will be used to clean the input DEM and burn bathymetry into the DEM. Setting this value to True will direct NenCarta to create a water mask for DEM cleaning that fills the stream cell with a specified depth of water. The depth of water will be based upon the value(s) specified in the `specify_depths_for_bathy_mask` argument. Default True.

* **`use_warning_flags_to_download_dem`** (Bool, optional): TODO. Default False.

* **`user_flow_files`** (String or List of Strings, optional): If `floodmap_mode` is "user", than use this file or list of files to create flood maps instead of looking at the forecast.

* **`vdt_file_extension`** (String, optional): The file extension of the VDT files to be created. Default "txt".

Once you have a JSON created. You can simply issue this command (assuming your conda environment is active in the command prompt):

```bash
flood-mapping json "/path/to/your.json" --serial
```

Using the json argument you can also specify whether you intend to run NenCarta in serial or parallel model using either `--serial` or `--parallel --num_workers 8`, where 8 is the number of workers or processes that you intend to utilize in your NenCarta
simulation.


And your NenCarta simulation will commense with the JSON file.

## Command Line Interface
The second option is to run an indivdual watershed straight from the command line. This can be done by issuing the following command:

```
flood-mapping cli ExampleWatershed "C:\path\to\flowline.shp" "C:\path\to\dem_dir" "C:\path\to\output" --bathy_use_banks --clean_dem --process_stream_network --mapper FloodSpreader --use_specified_depth_for_bathy_mask --specify_depths_for_bathy_mask 1.0 2.0 --age_of_forecast_days 7 --find_banks_based_on_landcover --create_reach_average_curve_file --forensic_forecast_date "20250807" --specified_bathyflow_field 'p_exceed_50' --specified_highflow_field "rp100_premium" --use_warning_flags_to_download_dem --geoglows_vpu 15 --lake_filter_json "C:\path\to\lake_filter_json" 
--estimate_consequences --streamflow_source "NWM_short_range" --nwm_api_key "YOUR_NWM_API_KEY"
```

The arguments `--bathy_use_banks`, `--clean_dem`, `--process_stream_network`, `--use_specified_depth_for_bathy_mask`,`--find_banks_based_on_landcover`, `--create_reach_average_curve_file`, `use_warning_flags_to_download_dem`, and `--estimate_consequences` are issued when you intend setting those options as True.

The argument `--specify_depths_for_bathy_mask` requires two float arguments if `clean_dem` is True and one float argument if `clean_dem` is False.

The cli option will only operate in serial.

## Graphical User Interface (GUI)
A built-in GUI can be accessed that will build the JSON file and simulate a serial simulation in NenCarta.

The GUI can be accessed using the command below.

```bash
flood-mapping gui
```
