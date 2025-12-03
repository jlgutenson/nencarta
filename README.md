# About NenCarta
NenCarta is a workflow designed to automate the creation of riverine flood inundation maps using the Automated Rating Curve (ARC) tool and Curve2Flood and streamflow from either the National Water Model or the GEOGLOWS ECWMF Streamflow Service. For the coterminous United States, NenCarta can then take the resulting flood inundaton maps and estimate direct flood consequences using Go-Consequences and the National Structure Inventory (NSI).

Here is an overview of how NenCarta produces flood inundation maps and performs consequence assessment.

[![NenCarta Overview](https://github.com/jlgutenson/nencarta/blob/main/workflow.png)](https://github.com/jlgutenson/nencarta/blob/main/workflow.png)

# Setting up NenCarta

Below are instructions for setting up NenCarta using Miniconda and Docker.

1. Create the necessary conda environment with the following command in a command prompt window:
```bash
conda create -c conda-forge -n nencarta_py310 python=3.10 numba=0.60 gdal pyarrow geopandas pandas netcdf4 cython dask fiona s3fs xarray zarr beautifulsoup4 dataretrieval geojson progress tqdm geoglows pygeos noise pillow=9.0.1 rasterio
```

For those (i.e., Mike) who have difficulty with the above, use these sets of installs:

```bash
conda create -n nencarta_py310 -c conda-forge python=3.10 pip
conda activate nencarta_py310
pip install numba==0.60
pip install pyarrow geopandas pandas netcdf4 cython dask fiona s3fs xarray zarr beautifulsoup4 dataretrieval geojson progress tqdm geoglows pygeos noise pillow==9.0.1 rasterio
conda install conda-forge::gdal
python -m pip uninstall numpy numba
python -m pip install numpy==2.0 numba
pip instqall --upgrade numpy
pip install networkx
pip install dask-expr
```

2. Activate the conda environment:
```bash
conda activate nencarta_py310
```

3. Install the Automated Rating Curve (ARC) by cloning the repository and installing using the instructions provided [in ARC's README](https://github.com/MikeFHS/automated-rating-curve).

4. Install the Curve2Flood by cloning the repository and installing using the instructions provided in [Curve2Flood's README](https://github.com/MikeFHS/curve2flood).

5. Clone this repository and return to the command prompt window and navigate to where the NenCarta repository was cloned locally.

6. Inside the NenCarta repository run:
```bash
pip install .
```
7. Optional, if you want to compute economic consequences using the resulting flood inundation map, install Go-Consequences creating a Go-Consequences Docker container using the Dockerfile found [in this repository](https://github.com/jlgutenson/nencarta/blob/main/Dockerfile.prod). The easiet way to do this is to install Docker (e.g., [Docker Desktop](https://www.docker.com/products/docker-desktop/)), navigate to where the NenCarta repository was cloned locally using the command prompt window, and running the command

```bash
docker build --progress=plain --file Dockerfile.prod -t go-consequences:latest .
```

8. You should be set up now. You can run `flood-mapping -h` in your command prompt to see if things installed properly. Have fun you dirty animals! 

# How to Run NenCarta

There are two ways to run NenCarta, either by providing a JSON input's filepath or via command line arguments.

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
            "streamflow_source": "NWM"
        }
    ]
}
```

These inputs are:
name (String): The name of the watershed you're modeling

flowline (String): A full filepath to the flowline shapefile that you'll be using to run NenCarta.

dem_dir (String): A full filepath to the directory containing one or more DEMs that you will be using as input in NenCarta.

bathy_use_banks (True/False): Setting this to True will allow the system to use estimated bank elevations to estimate bathymetry using the streamflow specified in specified_bathyflow_field. Setting this value to False will allow the system to use the estimated water surface elevation to estimate bathymetry the using the streamflow specified in specified_bathyflow_field.

clean_dem (True/False): Setting this value to True will allow the system to use the DEM Cleaner. Setting this value to False will bypass use of the DEM Cleaner.
mapper ("FloodSpreader"/"Curve2Flood"): Setting this value to either option will allow the system to use the specified software to perform bathymetry estimation and flood inundation mapping.

mapper (String): Here you're specifying if you're running "FloodSpreader" or "Curve2Flood" when performing bathymetry estimation and flood inundation mapping.

output_dir (String): The full filepath to the directory where your output will be saved.

process_stream_network (True/False):  Setting this value to True will direct the forecast system to take the flowline network (your flowline variable) and determine which flowlines are within each of your DEM tiles and download the necessary ECMWF GEOGLOWS Streamflow Service historic data for each DEM tile. Setting this value to False will bypass the creation of these system inputs and assumes that those inputs already exist.

use_specified_depth_for_bathy_mask (True/False): Setting this value to False will direct NenCarta to create a water mask that is a compilation of the stream network raster and the land use designated as water. This will be used to clean the input DEM and burn bathymetry into the DEM. Setting this value to True will direct NenCarta to create a water mask for DEM cleaning that fills the stream cell with a specified depth of water. The depth of water will be based upon the value(s) specified in the `specify_depths_for_bathy_mask` argument.

age_of_forecast_days (Integer): This is how old previous forecasts will be allowed to be based upon the current day. For example, specifying 7 days will delete all forecast flood inundation maps that are older than 7 days. 

find_banks_based_on_landcover (True/False): Setting this value to True will direct NenCarta to first try finding the banks using the land cover. If the stream cell is in a water pixel, NenCarta will direct ARC to search for the end of the water surrounding the stream cell and this will location will be designated as the banks. Setting this value to False will direct NenCarta and ARC to find the banks by assuming the channel is flat in the DEM and allowing ARC to find the banks when the flat water surface ends in the cross-section.

specify_depths_for_bathy_mask(list/float value(s)): If `use_specified_depth_for_bathy_mask` is True, the user must specify at least one depth value for FloodSpreaderPy to use when creating a water mask for the DEM cleaner and the bathymetry estimation processes. If `clean_dem` is False, this argument requires only one float value and that value will be used to create the bathymetry estimation water mask. If `clean_dem` is True, you will need to specify two float values, the first value is the depth used to create the water mask for the DEM cleaner and the second value is used to create the bathymetry estimation water mask.  The json processes accepts these inputs as a Python list. The command line interface accepts these as one or two separate values (i.e., --specify_depths_for_bathy_mask 1.0 2.0).

create_reach_average_curve_file (True/False): Setting this value to true instructs NenCarta to direct ARC to output the curve file by taking velocity, depth, and top-width estimates for all stream cells on an individual stream reach and creating the curve parameters for each stream cell. Setting this value to False instructs NenCarta to direct ARC to output the curve file using velocity, depth, and top-width estimates for each stream cell.

forensic_forecast_date (String): If you want to use a past forecast, you can specify the date here in YYYYMMDD format (e.g., "20250807" is August 7, 2025) in UTC. The archive of GEOGLOWS forecasts goes back to July 1, 2024. We are unsure of the current limitations for the National Water Model archive of forecasts.

forensic_forecast_hour (String): If you want to use a past National Water Model forecast, along with the forensic_forecast_date, you must also specify a forensic forecast hour. This is the hour the forecast was produced, in UTC. For the "NWM_short_range" forecast, the forensic forecast hour must be between 0 and 23, expressed as a string (e.g., "00"). For the "NWM_medium_range" forecast, the forensic forecast hour must be "00", "06", "12", or "18". For the "NWM_long_range" forecast, the forensic forecast hour must be "00".


specified_bathyflow_field (String): The field in the GEOGLOWS downloaded reanalysis data that will be provided to ARC to estimate bathmetry in each cross-section. For "GEOGLOWS" it must be one of "qout_median", "qout_max", "rp2", "rp5", "rp10", "rp25", "rp50", "rp100","qout_max_premium", or "rp100_premium". For "NWM" it must be one of "rp2", "rp5", "rp10", "rp25", "rp50", "rp100", or "rp100_premium".

specified_highflow_field (String): The field in the GEOGLOWS downloaded reanalysis data that will be provided to ARC as the highest flow used to estimate water surface elevation. For "GEOGLOWS" it must be one of "qout_median", "qout_max", "rp2", "rp5", "rp10", "rp25", "rp50", "rp100","qout_max_premium", or "rp100_premium". For "NWM" it must be one of "rp2", "rp5", "rp10", "rp25", "rp50", "rp100", or "rp100_premium".

lake_filter_json (String): The path to the GEOGLOWS json that describes which stream reaches are within a lake. Optional input. This is currently only functional for GEOGLOWS data.

estimate_consequences (True/False): Optional input. Setting this equal to true will utilize Go-Consequences and the National Structure Inventory (NSI) to perform consequence assessment for the area within each flood inundation map. This functionality is currently only available for the coterminous United States.

streamflow_source (String): Setting this equal to "GEOGLOWS" will force NenCarta to use GEOGLOWS retrospective and forecast streamflow data. The deafult is GEOGLOWS. Setting this to "NWM_short_range" will force NenCarta to use the National Water Model retrospective and short-range forecast streamflow data. Setting this to "NWM_medium_range" will force NenCarta to use the National Water Model retrospective and medium-range forecast streamflow data. Setting this to "NWM_long_range" will force NenCarta to use the National Water Model retrospective and long-range forecast streamflow data. 

Once you have a JSON created. You can simply issue this command (assuming your conda environment is active in the command prompt):

```bash
flood-mapping json "/path/to/your.json" --serial
```

Using the json argument you can also specify whether you intend to run NenCarta in serial or parallel model using either `--serial` or `--parallel --num_workers 8`, where 8 is the number of workers or processes that you intend to utilize in your NenCarta
simulation.


And your NenCarta simulation will commense with the JSON file.

The second option is to run an indivdual watershed straight from the command line. This can be done by issuing the following command:

```
flood-mapping cli ExampleWatershed "C:\path\to\flowline.shp" "C:\path\to\dem_dir" "C:\path\to\output" --bathy_use_banks --clean_dem --process_stream_network --mapper FloodSpreader --use_specified_depth_for_bathy_mask --specify_depths_for_bathy_mask 1.0 2.0 --age_of_forecast_days 7 --find_banks_based_on_landcover --create_reach_average_curve_file --forensic_forecast_date "20250807" --specified_bathyflow_field 'qout_median' --specified_highflow_field "rp100_premium" --use_warning_flags_to_download_dem --geoglows_vpu 15 --lake_filter_json "C:\path\to\lake_filter_json" 
--estimate_consequences --streamflow_source "NWM_short_range"
```

The arguments `--bathy_use_banks`, `--clean_dem`, `--process_stream_network`, `--use_specified_depth_for_bathy_mask`,`--find_banks_based_on_landcover`, `--create_reach_average_curve_file`, `use_warning_flags_to_download_dem`, and `--estimate_consequences` are issued when you intend setting those options as True.

The argument `--specify_depths_for_bathy_mask` requires two float arguments if `clean_dem` is True and one float argument if `clean_dem` is False.

The cli option will only operate in serial.


