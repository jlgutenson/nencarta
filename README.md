# About NenCarta
NenCarta is a workflow designed to automate the creation of riverine flood inundation maps using the Automated Rating Curve (ARC) tool and Curve2Flood and streamflow from either the National Water Model or the GEOGLOWS ECWMF Streamflow Service. For the coterminous United States, NenCarta can then take the resulting flood inundaton maps and estimate direct flood consequences using Go-Consequences and the National Structure Inventory (NSI).

Here is an overview of how NenCarta produces flood inundation maps and performs consequence assessment.

[![NenCarta Overview](https://github.com/jlgutenson/nencarta/blob/main/workflow.png)](https://github.com/jlgutenson/nencarta/blob/main/workflow.png)

# Documentation

Structured documentation and examples can be found at our [Read the Docs page](https://nencarta.readthedocs.io/en/latest/index.html). 

# Setting up NenCarta

Below are instructions for setting up NenCarta using Miniconda and Docker.

1. Clone this repository and return to the command prompt window and navigate to where the NenCarta repository was cloned locally.

2. Create the necessary conda environment with the following command:
```bash
conda env create -f environment.yaml
```

3. Activate the conda environment:
```bash
conda activate nencarta_py312
```

4. Use the nencarta_py312 to install the Automated Rating Curve (ARC) toolkit by cloning the repository and installing using the instructions provided [in ARC's README](https://github.com/MikeFHS/automated-rating-curve). You can ignore the instruction in ARC's README that discuss creating another conda environment. 

5. Use the nencarta_py312 to install Curve2Flood by cloning the repository and installing using the instructions provided in [Curve2Flood's README](https://github.com/MikeFHS/curve2flood). You can ignore the instruction in Curve2Flood's README that discuss creating another conda environment. 


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

## Graphical User Interface (GUI)
A built-in GUI can be accessed that will build the JSON file and simulate a serial simulation in NenCarta.

The GUI can be accessed using the command below.

```bash
flood-mapping gui
```

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

The JSON inputs are each described [here](https://nencarta.readthedocs.io/en/latest/configuration.html#json-inputs). 

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

