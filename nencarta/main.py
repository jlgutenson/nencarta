#conda env create -f **.yml
#conda activate ffs_esa_dc

# build-in imports
import glob
import os
import re
import sys
import json
import stat
import pprint
import argparse
import platform
import subprocess
import multiprocessing as mp
from typing import TextIO
from datetime import datetime, timedelta
from contextlib import redirect_stdout, redirect_stderr

# third-party imports
import numpy as np
import pandas as pd
import geopandas as gpd
from arc import Arc
from numba import njit
from osgeo import gdal
from pyproj import CRS
from shapely.geometry import box
from curve2flood import Curve2Flood_MainFunction
from arc.Create_GeoJSON import Run_Main_VDT_to_GEOJSON_Program_Stream_Vector

# local imports
from . import LOG
from . import gui_app
from .timer import Timer
from . import DEM_Cleaner
from .flood_folder import FloodFolder
from . import Hydroterrain_Processing
from . import esa_download_processing as ESA
from . import streamflow_processing as HistFlows
from . import Download_Process_ForecastData as ForecastFlows


def _resolve_parallel_settings(data, cli_parallel=None, cli_num_workers=None):
    """
    Resolve (parallel: bool, num_workers: int) from JSON content and optional
    CLI overrides. CLI overrides win when provided. Defaults to serial.
    """
    # Support both 'parallel' and legacy 'run_parallel' in JSON
    json_parallel = data.get("parallel", data.get("run_parallel"))
    json_workers = data.get("num_workers", data.get("workers"))

    # Choose final parallel flag
    parallel = cli_parallel if cli_parallel is not None else json_parallel
    if parallel is None:
        parallel = False  # safer, deterministic default

    # Choose final worker count
    num_workers = cli_num_workers if cli_num_workers is not None else json_workers
    if parallel:
        try:
            num_workers = int(num_workers) if num_workers is not None else (os.cpu_count() or 1)
        except (TypeError, ValueError):
            num_workers = os.cpu_count() or 1
        num_workers = max(1, num_workers)
    else:
        num_workers = 1

    return bool(parallel), num_workers

def process_streams_locations_in_dem(folder: FloodFolder, watershed_dict: dict, DEM: str, stream_id_field: str):
    # now we need to figure out if our DEM_StrmShp and DEM_Reanalysis_Flowfile exists and if not, create it
    if os.path.isfile(folder.DEM_StrmShp) and os.path.isfile(folder.DEM_Reanalsyis_FlowFile):
        LOG.info(folder.DEM_StrmShp + ' Already Exists')
        LOG.info(folder.DEM_Reanalsyis_FlowFile + ' Already Exists')
    else:
        # Before we get too far ahead, let's make sure that our DEM and Flowlines have the same coordinate system
        StrmShp_gdf = process_river_geometry(folder, watershed_dict, DEM) 
        # save the initial network as a geopackage
        StrmShp_gdf.to_file(folder.DEM_StrmShp, driver="GPKG")
    
    return


def get_rivids(folder: FloodFolder, watershed_dict: dict, DEM: str, stream_id_field: str) -> np.ndarray | None:

    StrmShp_gdf = gpd.read_file(folder.DEM_StrmShp, driver='GPKG')

    rivids, _ = HistFlows.Process_and_Write_Retrospective_Data_for_DEM_Tile(StrmShp_gdf, stream_id_field, folder, watershed_dict)

    if rivids is None or len(rivids) == 0:
        LOG.info('DEM_StrmShp is empty, returning None values')
        return None

    return rivids

def download_forecast_streamflows(watershed_dict: dict, folder: FloodFolder, rivids: np.ndarray, streamflow_source: str) -> str:
    # now lets download the forecast streamflows
    #Forecast flow data from GeoGLOWS
    # parquet_file_from_geoglows = 'v2-model-table.parquet'     #http://geoglows-v2.s3-website-us-west-2.amazonaws.com/#tables/
    if (forecastdate := watershed_dict.get('forensic_forecast_date')):
        LOG.info(f"Using forensic forecast date: {forecastdate}")
        if streamflow_source.upper().startswith("NWM"):
            forecasthour = watershed_dict.get('forensic_forecast_hour')
            LOG.info(f"Using forensic forecast hour: {forecasthour}")
        elif streamflow_source.upper() == "GEOGLOWS":
            forecasthour = None
        try:
            demfilename = os.path.basename(folder.DEM_File)
            if streamflow_source.upper() == "GEOGLOWS":
                ForecastFlowFile = os.path.join(folder.FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{streamflow_source}_forecast.csv')
            elif streamflow_source.upper().startswith("NWM"):
                ForecastFlowFile = os.path.join(folder.FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{forecasthour}_{streamflow_source}_forecast.csv')
            if not os.path.exists(ForecastFlowFile):
                ForecastFlows.Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, ForecastFlowFile, streamflow_source, watershed_dict['nwm_api_key'])
        except Exception as e:
            LOG.error('Could not process forensic forecast streamflow download, please check your date or try again later...')
            raise e
    else:
        # cycle through today through 12 days ago to find the most recent day with a forecast
        found = False
        for fd in range(0,13):
            for fh in range(0,24):
                try:
                    demfilename = os.path.basename(folder.DEM_File)
                    forecastdate, forecasthour = ForecastFlows.Get_Date_For_Forecast(fd, fh, streamflow_source) 
                    LOG.info(f"Attempting to download forecast for date: {forecastdate} and hour: {forecasthour}...")
                    # we only need the forecast date for GEOGLOWS, for NWM we need the forecast hour as well               
                    if streamflow_source.upper() == "GEOGLOWS":
                        ForecastFlowFile = os.path.join(folder.FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{streamflow_source}_forecast.csv')
                    elif streamflow_source.upper().startswith("NWM"):
                        ForecastFlowFile = os.path.join(folder.FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{forecasthour}_{streamflow_source}_forecast.csv')
                    if not os.path.exists(ForecastFlowFile):
                        ForecastFlows.Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, ForecastFlowFile, streamflow_source, watershed_dict['nwm_api_key'])
                    watershed_dict['forecastdate'] = forecastdate
                    watershed_dict['forecasthour'] = forecasthour
                    found = True
                    break
                except Exception as e:
                    LOG.error(f'Could not process forecast, moving back another day.. ({e})')
            if found:
                break  # break outer
    LOG.info('Forecast data save here: ' + ForecastFlowFile)

    return ForecastFlowFile

def Process_Geospatial_Data(folder: FloodFolder, watershed_dict: dict, DEM: str):
    #Get the Spatial Information from the DEM Raster
    (minx, miny, maxx, maxy, dx, dy, ncols, nrows, _, dem_projection) = Get_Raster_Details(folder.DEM_File)
    projWin_extents = [minx, maxy, maxx, miny]
    outputBounds = [minx, miny, maxx, maxy]  #https://gdal.org/api/python/osgeo.gdal.html

    #Check the coordinate system of the rasters and if they are not in meters or degrees, end with log an error message and stop processing
    unit_aliases = {
        'meter': 'meter',
        'meters': 'meter',
        'metre': 'meter',
        'metres': 'meter',
        'degree': 'degree',
        'degrees': 'degree'
    }
    raster_projections = {
        'DEM': dem_projection,
    }
    for raster_name, raster_projection in raster_projections.items():
        try:
            raster_crs = CRS.from_wkt(raster_projection)
        except Exception as ex:
            LOG.error(f'Unable to parse CRS for {raster_name} raster: {ex}')
            return

        axis_units = {(axis.unit_name or '').strip().lower() for axis in raster_crs.axis_info if axis is not None}
        axis_units.discard('')
        if not axis_units:
            LOG.error(f'Unable to determine CRS units for {raster_name} raster.')
            return

        invalid_units = [u for u in sorted(axis_units) if unit_aliases.get(u) not in {'meter', 'degree'}]
        if invalid_units:
            LOG.error(f'{raster_name} raster CRS units are not meters or degrees: {", ".join(invalid_units)}')
            return

   
    #Create Land Dataset
    if os.path.isfile(folder.LAND_File):
        LOG.info(folder.LAND_File + ' Already Exists')
    else: 
        LOG.info('Creating ' + folder.LAND_File) 
        Create_AR_LandRaster(folder.LandCoverFiles, folder.LAND_File, projWin_extents, dem_projection, ncols, nrows)

    #Describe the streamflow forecast source we are using
    streamflow_source = watershed_dict['streamflow_source']
    # set the ID used for the stream network
    stream_id_field, ds_stream_id_field = get_streamids_from_source(streamflow_source)
    if streamflow_source.upper().startswith("NWM") or streamflow_source.upper() == "GEOGLOWS":
        pass
    else:
        LOG.error(f"streamflow_source {streamflow_source} not recognized, please use either 'NWM' or 'GEOGLOWS'")
        raise ValueError(f"streamflow_source {streamflow_source} not recognized, please use either 'NWM' or 'GEOGLOWS'")
    
    #Initially process the stream network for the DEM we are using.
    if watershed_dict['process_stream_network'] is False:
        LOG.info("We are not processing the stream network using the DEM.")
    else:
        LOG.info("Initially processing the provided stream network with the DEM.")
        StrmShp_gdf = process_streams_locations_in_dem(folder, watershed_dict, DEM, stream_id_field)

    
    #Determine if we want to move the stream network using the move_stream_network_to_new_locations argument. 
    #The stream has to move if we are using FLDPLNpy
    move_stream_network_to_new_locations = bool(watershed_dict['move_stream_network_to_new_locations'])
    folder.setup_fldpln_files()
    if (move_stream_network_to_new_locations is True or watershed_dict['mapper'] == "FLDPLNpy") and watershed_dict['process_stream_network'] is True:
        # check if the stream threshold was provided, otherwise throw an error:
        if watershed_dict['new_strm_threshold_km2'] is None:
            LOG.error("The argument new_strm_threshold_km2 is required for both moving the stream network using the DEM and using FLPDLNpy. Please provide new_strm_threshold_km2.")
            raise ValueError("The argument new_strm_threshold_km2 is required for both moving the stream network using the DEM and using FLPDLNpy. Please provide new_strm_threshold_km2.")
        if os.path.exists(folder.flowdir) and os.path.exists(folder.flowacc) and os.path.exists(folder.filled_dem) and watershed_dict['process_stream_network'] is False:
            LOG.info("The flow direction/accumulation rasters already exist and will not be recreated...")
        elif watershed_dict['process_stream_network'] is True:
            Hydroterrain_Processing.create_flow_direction_and_flow_accumulation_raster(
                folder.DEM_File, folder.filled_dem, folder.Flow_Direction_Folder, folder.flowdir, folder.flowacc
            )
        if os.path.exists(folder.new_StrmShp) and os.path.exists(folder.new_catchment) and watershed_dict['process_stream_network'] is False:
            LOG.info("The flow direction/accumulation rasters already exist and will not be recreated...")
        elif watershed_dict['process_stream_network'] is True:
            Hydroterrain_Processing.create_catchments_and_flowlines_with_flow_direction_and_accumulation(
                                                                                                            folder.flowdir,
                                                                                                            folder.flowacc,
                                                                                                            folder.Flow_Direction_Folder,
                                                                                                            watershed_dict['new_strm_threshold_km2'],
                                                                                                            folder.new_StrmShp,
                                                                                                            folder.new_catchment
                                                                                                        )
            Hydroterrain_Processing.match_new_streams_to_old_streams(
                                                                        folder.new_StrmShp,
                                                                        folder.DEM_StrmShp,
                                                                        folder.new_StrmShp_matched,
                                                                        stream_id_field,
                                                                        ds_stream_id_field,
                                                                        min_match_score = watershed_dict['min_match_score'],
                                                                        max_centroid_distance_m = 2000.0,
                                                                        require_overlap = True,
                                                                        remove_detached_upstream = True,
                                                                        connectivity_tolerance_m = 30.0,
                                                                    )
        # Update what things are since we've decided to use FLDPLNpy or to move the stream network.
        original_dem_file = folder.DEM_File
        folder.DEM_File = folder.filled_dem
        folder.DEM_StrmShp = folder.new_StrmShp_matched

    # Get stream IDs and retrospective data, if we need it.
    if watershed_dict['process_stream_network'] is False:
        LOG.info("We have our stream network as we want it, skipping the downloading of reanalysis data.")
        StrmShp_gdf = gpd.read_file(folder.DEM_StrmShp, driver='GPKG')
        keep_ids = set(pd.to_numeric(StrmShp_gdf[stream_id_field], errors="coerce").dropna().astype(int).tolist())
        rivids = sorted(keep_ids)
    else:
        LOG.info("Downloading reanalysis data and finishing processing of the stream network.")
        rivids = get_rivids(folder, watershed_dict, DEM, stream_id_field)

    if rivids is None:
        return

    #Create the Stream Raster
    if os.path.isfile(folder.STRM_File) and watershed_dict['process_stream_network'] is False:
        LOG.info(folder.STRM_File + ' Already Exists')
    else:
        LOG.info('Creating ' + folder.STRM_File)
        Create_AR_StrmRaster(folder.DEM_StrmShp, folder.STRM_File, outputBounds, minx, miny, maxx, maxy, dx, dy, ncols, nrows, 'LINKNO')
    
    if os.path.isfile(folder.STRM_File_Clean) and watershed_dict['process_stream_network'] is False:
        LOG.info(folder.STRM_File_Clean + ' Already Exists')
    else:
        LOG.info('Creating ' + folder.STRM_File_Clean)
        Clean_STRM_Raster(folder.STRM_File, folder.STRM_File_Clean)

    if watershed_dict['floodmap_mode'] == 'forecast':
        FlowFile = download_forecast_streamflows(watershed_dict, folder, rivids, streamflow_source)
    elif watershed_dict['floodmap_mode'] == 'user':
        FlowFile = watershed_dict['user_flow_files']
        if isinstance(FlowFile, str):
            FlowFile = [FlowFile]
        elif not isinstance(FlowFile, list):
            LOG.error("user_flow_files must be a string or list of strings.")
            raise ValueError("user_flow_files must be a string or list of strings.")
        
        for ff in FlowFile:
            if not os.path.isfile(ff):
                LOG.error(f"User provided flow file does not exist: {ff}")
                raise FileNotFoundError(f"User provided flow file does not exist: {ff}")

    if watershed_dict['process_stream_network'] is True:
        #Create a Starting AutoRoute Input File
        LOG.info('Creating ARC Input File: ' + folder.ARC_FileName_Initial)
        #Create the Initial Flow
        LOG.info(f"Using the field '{watershed_dict['specified_bathyflow_field']}' for bathymetry estimation and '{watershed_dict['specified_highflow_field']}' for flood mapping...\n")

        Create_FlowFile(folder.DEM_Reanalsyis_FlowFile, folder.COMID_Q_File, 'COMID', 'rp2')

        # Create the Initial input file which is only used if cleaning the DEM
        if watershed_dict['clean_dem']: 
            Create_ARC_Model_Input_File_Initial_for_cleaning_dem(folder, watershed_dict, 'COMID')

        Create_ARC_Model_Input_File_Bathy(folder, watershed_dict, 'COMID')

    if watershed_dict['floodmap_mode'] == 'forecast':
        Forecast_Flood_Map, Forecast_Flood_Depth_Raster = Create_ARC_Model_Input_File_FloodForecast(FlowFile, folder, watershed_dict)
        folder.setup_flood_forecast_files(Forecast_Flood_Map, Forecast_Flood_Depth_Raster, FlowFile)
    elif watershed_dict['floodmap_mode'] == 'user':
        flood_maps = []
        depth_maps = []
        model_input_files = []
        for flow_file in FlowFile:
            floodmap, depthmap, model_input_file = create_input_file_for_user_flowfiles(folder, watershed_dict, flow_file)
            flood_maps.append(floodmap)
            depth_maps.append(depthmap)
            model_input_files.append(model_input_file)

        folder.setup_flood_user_files(flood_maps, depth_maps, FlowFile, model_input_files)
    
    return

def Create_FlowFile(MainFlowFile, FlowFileName, OutputID, Qparam):
    infile = open(MainFlowFile,'r')
    lines = infile.readlines()
    ls = lines[0].strip().split(',')
    q_val = 0
    c_val = 0
    for i in range(len(ls)):
        if ls[i]==Qparam:
            q_val=i
        if ls[i]==OutputID:
            c_val=i
    
    outfile = open(FlowFileName, 'w')
    outfile.write(OutputID + ',' + Qparam)
    
    for r in range(1,len(lines)):
        ls = lines[r].strip().split(',')
        out_str = '\n' + ls[c_val] + ',' + ls[q_val]
        outfile.write(out_str)
    outfile.close()
    return

def _write_arc_input_section(out_file: TextIO, folder: FloodFolder, watershed_dict: dict, COMID_Param: str, maybe_use_clean_dem: bool):
    bathy_args: dict = watershed_dict['bathy_args']
    out_file.write('#ARC_Inputs')
    out_file.write(f'\nDEM_File\t{folder.DEM_File_Clean if maybe_use_clean_dem else folder.DEM_File}') # use_clean_dem will auto pick the cleaned DEM if it was created, else we force using the original DEM
    out_file.write(f'\nStream_File\t{folder.STRM_File_Clean}')
    out_file.write(f'\nLU_Raster_SameRes\t{folder.LAND_File}')
    out_file.write(f'\nLU_Manning_n\t{folder.mannings_n_text_file}')
    out_file.write(f'\nFlow_File\t{folder.DEM_Reanalsyis_FlowFile}')
    out_file.write(f'\nFlow_File_ID\t{COMID_Param}')
    out_file.write(f"\nFlow_File_BF\t{watershed_dict['specified_bathyflow_field']}")
    out_file.write(f'\nFlow_File_QMax\t{watershed_dict["specified_highflow_field"]}')
    out_file.write(f'\nSpatial_Units\tdeg')
    out_file.write(f'\nX_Section_Dist\t{bathy_args["X_Section_Dist"]}')
    out_file.write(f'\nDegree_Manip\t{bathy_args["Degree_Manip"]}')
    out_file.write(f'\nDegree_Interval\t{bathy_args["Degree_Interval"]}')
    out_file.write(f'\nLow_Spot_Range\t{bathy_args["Low_Spot_Range"]}')
    out_file.write(f'\nStr_Limit_Val\t{bathy_args["Str_Limit_Val"]}')
    out_file.write(f'\nGen_Dir_Dist\t{bathy_args["Gen_Dir_Dist"]}')
    out_file.write(f'\nGen_Slope_Dist\t{bathy_args["Gen_Slope_Dist"]}')
    out_file.write(f'\nStream_Slope_Method\t{bathy_args["Stream_Slope_Method"]}')

def _write_fldpln_section(out_file: TextIO, folder: FloodFolder, watershed_dict: dict, use_bathy_flow_dir: bool = False):
    out_file.write('\n\n#FLDPLN_Specific_Inputs')
    out_file.write(f'\nUse_FLDPLN_Model\tTrue')
    out_file.write(f'\nFlow_Direction_File\t{folder.flowdir if use_bathy_flow_dir else folder.flowdir}')

def Create_ARC_Model_Input_File_Initial_for_cleaning_dem(folder: FloodFolder, watershed_dict: dict, COMID_Param: str):
    with open(folder.ARC_FileName_Initial, 'w') as out_file:
        _write_arc_input_section(out_file, folder, watershed_dict, COMID_Param, False)

        out_file.write('\n\n#VDT_Output_File_and_CurveFile')
        out_file.write(f'\nVDT_Database_NumIterations\t30')
        out_file.write(f'\nPrint_VDT_Database\t{folder.VDT_File_Initial}')
        out_file.write(f'\nPrint_Curve_File\t{folder.Curve_File_Initial}')
        out_file.write(f'\nReach_Average_Curve_File\t{watershed_dict["create_reach_average_curve_file"]}')
        
        out_file.write('\n\n#Mapper Input Data')
        out_file.write(f'\nStrmShp_File\t{folder.DEM_StrmShp}')
        out_file.write(f'\nComid_Flow_File\t{folder.COMID_Q_File}')
        out_file.write(f'\nFS_ADJUST_FLOW_BY_FRACTION\t1.0')
        out_file.write(f'\nBathy_Use_Banks\t{watershed_dict["bathy_use_banks"]}')
        if watershed_dict['flood_waterlc_and_strm_cells']:
            out_file.write(f"\nLAND_WaterValue\t{watershed_dict['land_watervalue']}")
        if watershed_dict['find_banks_based_on_landcover']:
            out_file.write(f'\nFindBanksBasedOnLandCover\t{watershed_dict["find_banks_based_on_landcover"]}')
        # out_file.write(f'\nFloodLocalOnly')
        if watershed_dict['use_specified_depth_for_bathy_mask']:
            if watershed_dict['mapper'] == "FLDPLNpy":
                _write_fldpln_section(out_file, folder, watershed_dict)
            out_file.write(f'\nOutFLD\t{folder.FloodMapFile_Initial}')
            out_file.write(f'\nOutSHP\t' + folder.FloodMapFile_Initial.replace('.tif', '.shp'))
            out_file.write(f'\nFloodSpreader_SpecifyDepth\t{watershed_dict["specify_depths_for_bathy_mask"][0]}')    


def Create_ARC_Model_Input_File_Bathy(folder: FloodFolder, watershed_dict: dict, COMID_Param: str):
    LOG.info('Creating ARC Input File: ' + folder.ARC_FileName_Bathy)

    with  open(folder.ARC_FileName_Bathy,'w') as out_file:
        _write_arc_input_section(out_file, folder, watershed_dict, COMID_Param, True)
        bathy_args: dict = watershed_dict['bathy_args']
        
        out_file.write('\n\n#VDT_Output_File_and_CurveFile')
        out_file.write(f'\nVDT_Database_NumIterations\t{bathy_args.get("VDT_Database_NumIterations", 30)}')
        out_file.write(f'\nPrint_VDT_Database\t{folder.VDT_File_Bathy}')
        if watershed_dict['make_curvefile']:
            out_file.write(f'\nPrint_Curve_File\t{folder.Curve_File_Bathy}')
        out_file.write(f'\nPrint_VDT_Test_File\t{folder.VDT_Test_File_Bathy}')
        if watershed_dict['make_ap_database']:
            out_file.write(f'\nPrint_AP_Database\t{folder.AP_File}')

        out_file.write(f'\nReach_Average_Curve_File\t{watershed_dict["create_reach_average_curve_file"]}')
        out_file.write('\n\n#Mapper Input Data')
        out_file.write(f'\nComid_Flow_File\t{folder.COMID_Q_File}')
        out_file.write(f'\nStrmShp_File\t{folder.DEM_StrmShp}')
        out_file.write(f'\nFS_ADJUST_FLOW_BY_FRACTION\t{bathy_args.get("FS_ADJUST_FLOW_BY_FRACTION", 1.0)}')
        # out_file.write(f'\nFloodLocalOnly')
        out_file.write(f'\nOutFLD\t{folder.FloodMapFile_Bathy}')
        out_file.write(f'\nOutSHP\t{folder.FloodMapFile_Bathy_SHP}')
        # out_file.write(f'\nOutSHP	' + FloodMapFile.replace('.tif', '_Bathy.gpkg'))
        out_file.write(f'\nTopWidthDistanceFactor\t{bathy_args.get("TopWidthDistanceFactor", 1.5)}')
        out_file.write(f'\nTW_MultFact\t{bathy_args.get("TW_MultFact", 1.5)}')
        out_file.write(f'\nTopWidthPlausibleLimit\t{bathy_args.get("TopWidthPlausibleLimit", 2000)}')
        if not bathy_args.get('Make_Output_GPKG', True):
            out_file.write('\nMake_Output_GPKG\tFalse')

        if watershed_dict['use_specified_depth_for_bathy_mask']:
            if watershed_dict['mapper'] == "FLDPLNpy":
                _write_fldpln_section(out_file, folder, watershed_dict, True)
            if len(watershed_dict['specify_depths_for_bathy_mask']) == 1:
                specified_depth = watershed_dict['specify_depths_for_bathy_mask'][0]
            if len(watershed_dict['specify_depths_for_bathy_mask']) == 2:
                specified_depth = watershed_dict['specify_depths_for_bathy_mask'][1]
            out_file.write('\n' + f'FloodSpreader_SpecifyDepth\t{specified_depth}')
        elif not watershed_dict['use_specified_depth_for_bathy_mask']:
            out_file.write(f'\nBathyWaterMask\t{folder.FloodMapFile_Initial}')

        out_file.write('\n\n#Bathymetry_Information')
        out_file.write(f'\nBathy_Trap_H\t{bathy_args.get("Bathy_Trap_H", 0.2)}')
        out_file.write(f'\nBathy_Use_Banks\t{watershed_dict["bathy_use_banks"]}')

        if watershed_dict['flood_waterlc_and_strm_cells']:
            out_file.write(f'\nFlood_WaterLC_and_STRM_Cells\tTrue')
            out_file.write(f'\nLAND_WaterValue\t{watershed_dict["land_watervalue"]}')
        if watershed_dict['find_banks_based_on_landcover']:
            out_file.write(f'\nFindBanksBasedOnLandCover\tTrue')

        out_file.write(f'\nAROutBATHY\t{folder.ARC_BathyFile}')
        out_file.write(f'\nBATHY_Out_File\t{folder.ARC_BathyFile}')
        out_file.write(f'\nFSOutBATHY\t{folder.FS_BathyFile}')


def write_first_floodmap_inputs(out_file: TextIO, folder: FloodFolder, watershed_dict: dict, flow_file: str):
    floodmap_args: dict = watershed_dict['floodmap_args']
    out_file.write('#ARC_Inputs')
    out_file.write(f'\nDEM_File\t{folder.FS_BathyFile}')
    out_file.write(f'\nStream_File\t{folder.STRM_File_Clean}')
    out_file.write(f'\nLU_Manning_n\t{folder.mannings_n_text_file}')
    
    out_file.write('\n\n#VDT_Output_File_and_CurveFile')
    out_file.write(f'\nPrint_VDT_Database\t{folder.VDT_File_Bathy}')
    if watershed_dict['make_curvefile']:
        out_file.write(f'\nPrint_Curve_File\t{folder.Curve_File_Bathy}')
    
    out_file.write('\n\n#Mapper Input Data')
    out_file.write(f'\nStrmShp_File\t{folder.DEM_StrmShp}')
    out_file.write(f'\nComid_Flow_File\t{flow_file}')
    if watershed_dict['mapper'] == "FLDPLNpy":
        _write_fldpln_section(out_file, folder, watershed_dict, True)
    out_file.write(f'\nFS_ADJUST_FLOW_BY_FRACTION\t{floodmap_args.get("FS_ADJUST_FLOW_BY_FRACTION", 1.0)}')
    out_file.write(f'\nTW_MultFact\t{floodmap_args.get("TW_MultFact", 1.5)}')
    out_file.write(f'\nTopWidthPlausibleLimit\t{floodmap_args.get("TopWidthPlausibleLimit", 6000)}')
    if not floodmap_args.get('Make_Output_GPKG', True):
        out_file.write('\nMake_Output_GPKG\tFalse')

def write_floodmap_outputs(out_file: TextIO, files: list[str], watershed_dict: dict):
    flood_file, floodmap_geometry_file, depth_file, wse_file, velocity_file = files
    if watershed_dict['overwrite_floodmaps']:
        for file in [flood_file, floodmap_geometry_file, depth_file, wse_file, velocity_file]:
            if os.path.exists(file):
                os.remove(file)

    out_file.write(f'\nOutFLD\t{flood_file}')
    if watershed_dict['floodmap_args'].get('Make_Output_GPKG', True):
        out_file.write(f'\nOutSHP\t{floodmap_geometry_file}')
    if watershed_dict['make_depth_maps']:
        out_file.write(f'\nOutDEP\t{depth_file}')
    if watershed_dict['make_wse_maps']:
        out_file.write(f'\nOutWSE\t{wse_file}')
    if watershed_dict['make_velocity_maps']:
        out_file.write(f'\nOutVEL\t{velocity_file}')

def create_input_file_for_user_flowfiles(folder: FloodFolder, watershed_dict: dict, flow_file: str):
    file_postfix = f"_{os.path.basename(flow_file).rsplit('.', 1)[0]}.tif"
    model_input_file = os.path.join(folder.ARC_Folder, f"{watershed_dict['streamflow_source']}_ARC_Input_{folder.FileName}{folder.floodmap_id}_{file_postfix.replace('.tif', '.txt')}")
    LOG.info('Creating ARC Input File: ' + model_input_file)
    
    with open(model_input_file, 'w') as f:
        write_first_floodmap_inputs(f, folder, watershed_dict, flow_file)
        
        depth_file = folder.FloodDepthFile.replace('.tif', file_postfix)
        wse_file = folder.FloodWSEFile.replace('.tif', file_postfix)
        velocity_file = folder.FloodVELFile.replace('.tif', file_postfix)
        flood_file = folder.FloodMapFile.replace('.tif', file_postfix)
        floodmap_geometry_file = flood_file.replace('.tif','.shp')

        if watershed_dict['flood_waterlc_and_strm_cells'] or folder.FloodVELFile:
            f.write(f'\nFlood_WaterLC_and_STRM_Cells\t{str(watershed_dict["flood_waterlc_and_strm_cells"])}')
            f.write(f'\nLU_Raster_SameRes\t{folder.LAND_File}')
            f.write(f'\nLAND_WaterValue\t{str(watershed_dict["land_watervalue"])}')

        write_floodmap_outputs(f, [flood_file, floodmap_geometry_file, depth_file, wse_file, velocity_file], watershed_dict)


    return (flood_file, depth_file, model_input_file)

def Create_ARC_Model_Input_File_FloodForecast(ForecastFlowFile: str, folder: FloodFolder, watershed_dict: dict):
    LOG.info('Creating ARC Input File: ' + folder.ARC_FileName_FloodForecast)

    with open(folder.ARC_FileName_FloodForecast, 'w') as out_file:
        write_first_floodmap_inputs(out_file, folder, watershed_dict, ForecastFlowFile)

        #out_file.write(f'\nFloodLocalOnly')
        if watershed_dict['streamflow_source'].upper().startswith("NWM"):
            # create the end of the file name that describes the forecast
            if watershed_dict['forensic_forecast_date'] != None:
                ending_of_forecast_file = '_Forecast_' + str(watershed_dict['forensic_forecast_date']) + '_' + str(watershed_dict['forensic_forecast_hour']) + '.tif' 
            elif watershed_dict['forecastdate'] != None:
                ending_of_forecast_file = '_Forecast_' + str(watershed_dict['forecastdate']) + '_' + str(watershed_dict['forecasthour']) + '.tif' 
            # rename the forecast of the extent raster based upon the type of NWM forecast we are using
            Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('.tif', ending_of_forecast_file)
            Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('NWM', watershed_dict['streamflow_source'])
        elif watershed_dict['streamflow_source'].upper().startswith("GEOGLOWS"):
            # create the end of the file name that describes the forecast
            if watershed_dict['forensic_forecast_date'] != None:
                ending_of_forecast_file = '_Forecast_' + str(watershed_dict['forensic_forecast_date']) + '.tif'
            elif watershed_dict['forecastdate'] != None:
                ending_of_forecast_file = '_Forecast_' + str(watershed_dict['forecastdate']) + '.tif'
            Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('.tif', ending_of_forecast_file)
        
        Forecast_Flood_Depth_Raster = folder.FloodDepthFile.replace('.tif', ending_of_forecast_file)
        Forecast_Flood_WSE_Raster = folder.FloodWSEFile.replace('.tif', ending_of_forecast_file)
        Forecast_Flood_VEL_Raster = folder.FloodVELFile.replace('.tif', ending_of_forecast_file)
        Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('.tif', ending_of_forecast_file)
        Forecast_Flood_Map_Shapefile = Forecast_Flood_Map_Raster.replace('.tif','.shp')

        if watershed_dict['flood_waterlc_and_strm_cells'] or folder.FloodVELFile:
            out_file.write(f'\nFlood_WaterLC_and_STRM_Cells\t{str(watershed_dict["flood_waterlc_and_strm_cells"])}')
            out_file.write(f'\nLU_Raster_SameRes	' + folder.LAND_File)
            out_file.write(f'\nLAND_WaterValue\t{str(watershed_dict["land_watervalue"])}')

        write_floodmap_outputs(out_file, [Forecast_Flood_Map_Raster, Forecast_Flood_Map_Shapefile, Forecast_Flood_Depth_Raster, Forecast_Flood_WSE_Raster, Forecast_Flood_VEL_Raster], watershed_dict)

    return (Forecast_Flood_Map_Raster, Forecast_Flood_Depth_Raster)
    
def Create_BaseLine_Manning_n_File_ESA(ManningN):
    LOG.info('Creating Manning n file: ' + ManningN)
    with open(ManningN,'w') as out_file:
        out_file.write('LC_ID	Description	Manning_n')
        out_file.write(f'\n10	Tree Cover	0.120')
        out_file.write(f'\n20	Shrubland	0.050')
        out_file.write(f'\n30	Grassland	0.030')
        out_file.write(f'\n40	Cropland	0.035')
        out_file.write(f'\n50	Builtup	0.075')
        out_file.write(f'\n60	Bare	0.030')
        out_file.write(f'\n70	SnowIce	0.030')
        out_file.write(f'\n80	Water	0.030')
        out_file.write(f'\n90	Emergent_Herb_Wet	0.100')
        out_file.write(f'\n95	Mangroves	0.100')
        out_file.write(f'\n100	MossLichen	0.100')

def Create_AR_LandRaster(LandCoverFiles, LAND_File, projWin_extents, out_projection, ncols, nrows):
    options = gdal.WarpOptions(
        format='GTiff',
        outputBounds=projWin_extents,
        outputBoundsSRS=out_projection,
        width=ncols,
        height=nrows,
        dstSRS=out_projection,
        resampleAlg='mode', # Best for categorical data
        creationOptions=['COMPRESS=DEFLATE']
    )

    gdal.Warp(LAND_File, LandCoverFiles, options=options)
    return

def Create_AR_StrmRaster(StrmSHP, STRM_File, outputBounds, minx, miny, maxx, maxy, dx, dy, ncols, nrows, Param):
    """
    Converts a vector stream dataset (GeoPackage or Shapefile) into a raster.

    Parameters:
    - StrmSHP (str): Path to the vector dataset (GeoPackage or Shapefile)
    - STRM_File (str): Output raster file (GeoTIFF)
    - outputBounds (tuple): (minX, minY, maxX, maxY) defining the raster extent
    - minx, miny, maxx, maxy: Bounding box coordinates (not explicitly needed if outputBounds is set)
    - dx, dy: Resolution in X and Y directions
    - ncols, nrows: Number of columns and rows
    - Param (str): Attribute field to use for rasterization

    Returns:
    - None
    """
    LOG.info(f"Processing: {StrmSHP}")

    # Open vector dataset (supports both .shp and .gpkg)
    source_ds = gdal.OpenEx(StrmSHP, gdal.OF_VECTOR)
    if source_ds is None:
        LOG.error(f"Error: Could not open {StrmSHP}")
        return

    # Get the first (only) layer
    layer = source_ds.GetLayer(0)
    if layer is None:
        LOG.error(f"Error: No layers found in {StrmSHP}")
        return

    layer_name = layer.GetName()  # Get the actual layer name

    # Rasterization
    gdal.Rasterize(
        STRM_File, source_ds, format="GTiff", outputType=gdal.GDT_Int32,
        outputBounds=outputBounds, width=ncols, height=nrows,
        noData=-9999, attribute=Param,
        layers=[layer_name] if StrmSHP.lower().endswith(".gpkg") else None,  # Fix layers param
        creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2"]
    )

    # Clean up
    source_ds = None
    LOG.info(f"Rasterization complete: {STRM_File}")

    return

def Write_Output_Raster(s_output_filename, raster_data, ncols, nrows, dem_geotransform, dem_projection, s_file_format, s_output_type):   
    o_driver: gdal.Driver = gdal.GetDriverByName(s_file_format)  #Typically will be a GeoTIFF "GTiff"
    
    # Construct the file with the appropriate data shape
    o_output_file: gdal.Dataset = o_driver.Create(s_output_filename, xsize=ncols, ysize=nrows, bands=1, eType=s_output_type, options=['COMPRESS=DEFLATE'])

    # Get the first band (assuming a single-band raster)
    band = o_output_file.GetRasterBand(1)

    # Initialize the band with zeros
    band.Fill(0)

    # Write to disk
    band.FlushCache()
    
    # Set the geotransform
    o_output_file.SetGeoTransform(dem_geotransform)
    
    # Set the spatial reference
    o_output_file.SetProjection(dem_projection)
    
    # Write the data to the file
    o_output_file.GetRasterBand(1).WriteArray(raster_data)
    
    # Once we're done, close properly the dataset
    o_output_file = None


def Get_Raster_Details(DEM_File):
    LOG.info(f"Getting raster details for: {DEM_File}")
    gdal.Open(DEM_File, gdal.GA_ReadOnly)
    data = gdal.Open(DEM_File)
    geoTransform = data.GetGeoTransform()
    ncols = int(data.RasterXSize)
    nrows = int(data.RasterYSize)
    minx = geoTransform[0]
    dx = geoTransform[1]
    maxy = geoTransform[3]
    dy = geoTransform[5]
    maxx = minx + dx * ncols
    miny = maxy + dy * nrows
    Rast_Projection = data.GetProjectionRef()
    data = None
    return minx, miny, maxx, maxy, dx, dy, ncols, nrows, geoTransform, Rast_Projection


def Read_Raster_GDAL(InRAST_Name):
    try:
        dataset = gdal.Open(InRAST_Name, gdal.GA_ReadOnly)
    except RuntimeError:
        sys.exit(" ERROR: Field Raster File cannot be read!")

    # Retrieve dimensions and geospatial metadata.
    geotransform = dataset.GetGeoTransform()
    band = dataset.GetRasterBand(1)
    RastArray = band.ReadAsArray()
    ncols = band.XSize
    nrows = band.YSize
    band = None

    # Normalize south-up rasters (pixel height > 0) to north-up arrays.
    if geotransform[5] > 0:
        LOG.warning('Raster appears south-up (positive pixel height); flipping to north-up: ' + str(InRAST_Name))
        RastArray = np.flipud(RastArray)
        geotransform = (
            geotransform[0],
            geotransform[1],
            geotransform[2],
            geotransform[3] + geotransform[5] * nrows,
            geotransform[4],
            -geotransform[5],
        )

    cellsize = geotransform[1]
    yll = geotransform[3] - nrows * np.fabs(geotransform[5])
    yur = geotransform[3]
    xll = geotransform[0]
    xur = xll + (ncols) * geotransform[1]
    lat = np.fabs((yll + yur) / 2.0)
    Rast_Projection = dataset.GetProjectionRef()
    dataset = None

    LOG.debug('Spatial Data for Raster File:')
    LOG.debug('   ncols = ' + str(ncols))
    LOG.debug('   nrows = ' + str(nrows))
    LOG.debug('   cellsize = ' + str(cellsize))
    LOG.debug('   yll = ' + str(yll))
    LOG.debug('   yur = ' + str(yur))
    LOG.debug('   xll = ' + str(xll))
    LOG.debug('   xur = ' + str(xur))
    return RastArray, ncols, nrows, cellsize, yll, yur, xll, xur, lat, geotransform, Rast_Projection


def _clean_stream_raster(SN: np.ndarray, ncols: int, nrows: int):
    #Create an array that is slightly larger than the STRM Raster Array
    B = np.zeros((nrows+2,ncols+2), dtype=np.int64)
    
    #Imbed the STRM Raster within the Larger Zero Array
    # B[1:(nrows+1), 1:(ncols+1)] = SN
    B[...] = np.pad(SN, pad_width=1, mode='constant')

    
    #Added this because sometimes the non-stream values end up as -9999
    B = np.where(B>0,B,0)
    #(RR,CC) = B.nonzero()
    (RR,CC) = np.where(B>0)
    num_nonzero = len(RR)
    first_pass = 0
    second_pass = 0
    
    for filterpass in range(2):
        #First pass is just to get rid of single cells hanging out not doing anything
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
            r=RR[x]
            c=CC[x]
            V = B[r,c]
            if V>0:
                #Left and Right cells are zeros
                if B[r,c+1]==0 and B[r,c-1]==0:
                    #The bottom cells are all zeros as well, but there is a cell directly above that is legit
                    if (B[r+1,c-1]+B[r+1,c]+B[r+1,c+1])==0 and B[r-1,c]>0:
                        B[r,c] = 0
                        first_pass += 1
                    #The top cells are all zeros as well, but there is a cell directly below that is legit
                    elif (B[r-1,c-1]+B[r-1,c]+B[r-1,c+1])==0 and B[r+1,c]>0:
                        B[r,c] = 0
                        first_pass += 1
                #top and bottom cells are zeros
                if B[r,c]>0 and B[r+1,c]==0 and B[r-1,c]==0:
                    #All cells on the right are zero, but there is a cell to the left that is legit
                    if (B[r+1,c+1]+B[r,c+1]+B[r-1,c+1])==0 and B[r,c-1]>0:
                        B[r,c] = 0
                        first_pass += 1
                    elif (B[r+1,c-1]+B[r,c-1]+B[r-1,c-1])==0 and B[r,c+1]>0:
                        B[r,c] = 0
                        first_pass += 1
        
        #This pass is to remove all the redundant cells
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
            r=RR[x]
            c=CC[x]
            V = B[r,c]
            if V>0:
                if B[r+1,c]==V and (B[r+1,c+1]==V or B[r+1,c-1]==V):
                    if sum(B[r+1,c-1:c+2])==0:
                        B[r+1,c] = 0
                        second_pass += 1
                elif B[r-1,c]==V and (B[r-1,c+1]==V or B[r-1,c-1]==V):
                    if sum(B[r-1,c-1:c+2])==0:
                        B[r-1,c] = 0
                        second_pass += 1
                elif B[r,c+1]==V and (B[r+1,c+1]==V or B[r-1,c+1]==V):
                    if sum(B[r-1:r+1,c+2])==0:
                        B[r,c+1] = 0
                        second_pass += 1
                elif B[r,c-1]==V and (B[r+1,c-1]==V or B[r-1,c-1]==V):
                    if sum(B[r-1:r+1,c-2])==0:
                            B[r,c-1] = 0
                            second_pass += 1

    return B, first_pass, second_pass

def Clean_STRM_Raster(STRM_File, STRM_File_Clean):
    LOG.info('\nCleaning up the Stream File.')
    (SN, ncols, nrows, _, _, _, _, _, _, dem_geotransform, dem_projection) = Read_Raster_GDAL(STRM_File)

    B, first_pass, second_pass = _clean_stream_raster(SN, ncols, nrows)
    LOG.info(f'First Pass - Removed {first_pass} cells.')
    LOG.info(f'Second Pass - Removed {second_pass} cells.')
    
    LOG.info('Writing Output File ' + STRM_File_Clean)
    Write_Output_Raster(STRM_File_Clean, B[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Int32)
    return

def Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(folder: FloodFolder, watervalue):
    if os.path.exists(folder.LU_and_Streams_Water_Map):
        LOG.info(f"{folder.LU_and_Streams_Water_Map} exists and we aren't making it again...")
        return
    
    LOG.info('Cannot find initial flood file, so creating ' + folder.LU_and_Streams_Water_Map)
    (LC, ncols, nrows, _, _, _, _, _, _, _, _) = Read_Raster_GDAL(folder.LAND_File)
    (SN, ncols, nrows, _, _, _, _, _, _, sn_geotransform, sn_projection) = Read_Raster_GDAL(folder.STRM_File_Clean)
    
    '''
    # Streams identified in LC
    LC = np.where(LC == watervalue, 1, -9999)   # Mark streams with 1, other areas as -9999
    
    # Streams identified in SN
    SN = np.where(SN > 0, 1, 0)  # Mark streams with 1, other areas with 0
    
    # Combine LC and SN values
    F = np.where(SN == 1, 1, LC)  # Prioritize SN stream values, else take LC
    
    # Mark non-stream areas as -9999 in the final flood map
    F = np.where(F > 0, 1, -9999)
    '''
    # Mark streams in LC with 1, other areas as -9999
    # Mark streams in SN with 1, other areas with 0
    # Combine LC and SN, prioritizing SN values
    # Mark non-stream areas as -9999 in the final flood map

    F = np.full_like(LC, -9999, dtype=np.int16)
    mask = (SN > 0) | (LC == watervalue)
    F[mask] = 1
    
    Write_Output_Raster(folder.LU_and_Streams_Water_Map, F, ncols, nrows, sn_geotransform, sn_projection, "GTiff", gdal.GDT_Int16)

    return

def Create_Go_Consequence_GeoJSON(Consequences_JSON_Path, Forecast_Flood_Depth_Raster_Name, Consequences_Output_GPKG_File):
    """
    Write a go-consequences config JSON using NSIAPI structures,
    one 'depth' hazard GeoTIFF, and a GPKG results writer.

    Args:
        Consequences_GeoJSON_Path (str): Full path (including filename) to write the JSON config on your host.
        FloodDepthFile (str): Container-visible path to the flood-depth GeoTIFF (e.g., '/data/.../file.tif').
        Consequences_GeoJSON_File (str): Container-visible output GPKG path (e.g., '/data/.../file.gpkg').

    Returns:
        None
    """

    config = {
        "structure_provider_info": {
            "structure_provider_type": "NSIAPI"
        },
        "hazard_provider_info": {
            "hazards": [
                {
                    "hazard_parameter_type": "depth",
                    "hazard_provider_file_path": f"/data/FloodMap/{Forecast_Flood_Depth_Raster_Name}"
                }
            ]
        },
        "results_writer_info": {
            "results_writer_type": "GPKG",
            "output_file_path": f"/data/Consequences/{Consequences_Output_GPKG_File}"
        }
    }

    with open(Consequences_JSON_Path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

    return

def create_fist_inputs(folder: FloodFolder, watershed_dict: dict, timer: Timer):
    OutProjection = "EPSG:4269"
    stream_id_field, ds_stream_id_field = get_streamids_from_source(watershed_dict['streamflow_source'])

    flow_files = folder.get_flow_files()
    for flow_file in flow_files:
        # SEED file for creating a GEOJSON for FIST
        if watershed_dict['floodmap_mode'] == 'forecast':
            # There will be only one forecast file being used here
            SEED_File = os.path.join(folder.FIST_Folder, folder.FileName + '_Seed.shp') 
        elif watershed_dict['floodmap_mode'] == 'user':
            SEED_File = os.path.join(folder.FIST_Folder, f"{folder.FileName}_{os.path.basename(flow_file).rsplit('.', 1)[0]}_Seed.shp")

        streamflow_forecast_df = pd.read_csv(flow_file)
        streamflow_columns = streamflow_forecast_df.select_dtypes(include=['float']).columns.tolist()

        # grab the stream id column, which should be the first column in the flow file and should be named 'rivid' or 'comid' depending on the source of streamflow data
        id_column_name = streamflow_forecast_df.columns[0]

        for streamflow_column in streamflow_columns:            
            streamflow_forecast_filtered_df = streamflow_forecast_df[[id_column_name, streamflow_column]]
            if watershed_dict['floodmap_mode'] == 'forecast':
                GeoJSON_File = os.path.join(folder.FIST_Folder, f"{folder.FileName}_{watershed_dict['forensic_forecast_date']}_{streamflow_column}.geojson") 
            elif watershed_dict['floodmap_mode'] == 'user':
                GeoJSON_File = os.path.join(folder.FIST_Folder, f"{folder.FileName}_{os.path.basename(flow_file).rsplit('.', 1)[0]}_{streamflow_column}.geojson")

            if os.path.exists(GeoJSON_File):
                continue

            LOG.info('Creating FIST Input: ' + GeoJSON_File)
            with timer('geojson_fist'):
                Run_Main_VDT_to_GEOJSON_Program_Stream_Vector(folder.VDT_File_Bathy, folder.STRM_File_Clean, GeoJSON_File, OutProjection, folder.DEM_StrmShp, stream_id_field, ds_stream_id_field, SEED_File, Thin_Output=True, comid_q_df=streamflow_forecast_filtered_df)

def remove_old_forecast_files(folder: FloodFolder, watershed_dict: dict):
    if not watershed_dict['remove_old_forecast_files']:
        return
    
    # check and see if forecasts past a specified date exist and if so, delete them
    forecast_dir = os.path.dirname(folder.Forecast_Flood_Map)
    forecast_file = os.path.basename(folder.Forecast_Flood_Map)
    files_in_forecast_dir = os.listdir(forecast_dir)
    for filename in files_in_forecast_dir:
        if not filename.startswith(forecast_file[:-12]):
            continue

        # Regular expression to extract the date
        date_pattern = re.compile(r'\d{8}')
        match = date_pattern.search(filename)

        if not match:
            LOG.warning("No valid date found in the filename.")
            continue

        # Extracted date string
        date_str = match.group()
        
        # Convert to datetime object
        file_date = datetime.strptime(date_str, '%Y%m%d')
        
        # Calculate the date 7 days ago
        seven_days_ago = datetime.now() - timedelta(days=watershed_dict['age_of_forecast_days'])
        
        # Check if the file is older than 7 days
        if file_date <= seven_days_ago:
            # If the file is 7 days old or older, delete the file
            os.remove(os.path.join(forecast_dir,filename))
            LOG.info(f"File {filename} has been deleted.")
        else:
            LOG.info(f"File {filename} is not old enough to be deleted.")
                

def run_dem_cleaner(folder: FloodFolder, watershed_dict: dict, timer: Timer, DEM: str):
    # Run the DEM Cleaner Program, if you wanna
    if os.path.exists(folder.DEM_File_Clean) or not watershed_dict['clean_dem'] or watershed_dict['process_stream_network'] is False:
        return
    
    if not os.path.exists(folder.Curve_File_Initial):
        # start time for the simulation
        with timer('arc_initial'):
            arc = Arc(folder.ARC_FileName_Initial, quiet=watershed_dict['quiet'])
            arc.run() 
    else:
        LOG.info(f"{folder.Curve_File_Initial} exists and we aren't making it again...")

    with timer('initial_flood_for_cleaner'):
        if watershed_dict['mapper'] == "FloodSpreader" and watershed_dict['use_specified_depth_for_bathy_mask']:
            # Resolve the path to floodspreader.py
            script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
            floodspreader_path = os.path.join(script_dir, "floodspreader.py")
            call_mapper = f'python "{floodspreader_path}" {folder.ARC_FileName_Initial}'
            subprocess.call(call_mapper, shell=True)
        elif (watershed_dict['mapper'] in ["Curve2Flood", "FLDPLNpy"]) and watershed_dict['use_specified_depth_for_bathy_mask']:
            LOG.info(f"Executing Curve2Flood using {folder.ARC_FileName_Initial}")
            Curve2Flood_MainFunction(folder.ARC_FileName_Initial, quiet=watershed_dict['quiet'])
    
    OutputID = 'COMID'
    Q_Fraction = 0.10
    TopWidthPlausibleLimit = 600
    search_dist_for_min_elev = 10
    search_dist_perp_cells = 10 # this was 40
    FlowFileName = os.path.join(folder.FLOW_Folder, folder.FileName + '_Flow_COMID_Q.txt')
    Create_FlowFile(folder.DEM_Reanalsyis_FlowFile, FlowFileName, OutputID, 'p_exceed_50')
    # start time for the simulation
    with timer('dem_cleaner'):
        DEM_Cleaner.DEM_Cleaner_Program(OutputID, 
                                        folder.DEM_StrmShp, 
                                        folder.dem_folder, 
                                        [DEM], 
                                        [folder.STRM_File_Clean], 
                                        folder.dem_updated_folder, 
                                        FlowFileName, 
                                        folder.Curve_File_Initial, 
                                        folder.LU_and_Streams_Water_Map, 
                                        Q_Fraction, 
                                        TopWidthPlausibleLimit, 
                                        search_dist_for_min_elev, 
                                        search_dist_perp_cells)

def create_bathymetry(folder: FloodFolder, watershed_dict: dict, timer: Timer):
    # Create a Bathymetry Raster Dataset
    if not os.path.exists(folder.FS_BathyFile) or watershed_dict['process_stream_network'] is True:
        LOG.info('Cannot find bathy file, so creating ' + folder.FS_BathyFile)
        if not os.path.exists(folder.ARC_BathyFile):
            # start time for the simulation
            with timer('arc_bathy'):
                arc = Arc(folder.ARC_FileName_Bathy, quiet=watershed_dict['quiet'])
                arc.run()
        else:
            LOG.info(f"{folder.ARC_BathyFile} exists and we aren't making it again...")   

        with timer('flood_bathy'):
            if watershed_dict['mapper'] == "FloodSpreader":
                # Resolve the path to floodspreader.py
                script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
                floodspreader_path = os.path.join(script_dir, "floodspreader.py")
                # Build the subprocess call with the full path
                call_mapper = f'python "{floodspreader_path}" {folder.ARC_FileName_Bathy}'
                subprocess.call(call_mapper, shell=True)
            elif (watershed_dict['mapper'] in ["Curve2Flood", "FLDPLNpy"]):
                LOG.info(f"Executing Curve2Flood using {folder.ARC_FileName_Bathy}")
                Curve2Flood_MainFunction(folder.ARC_FileName_Bathy, quiet=watershed_dict['quiet'])
    else:
        LOG.info(f"{folder.FS_BathyFile} exists and we aren't making it again...")

def run_flood_mapper(watershed_dict: dict, folder: FloodFolder, timer: Timer, input_file: str):
    with timer('flood'):
        if watershed_dict['mapper'] == "FloodSpreader":
            # Resolve the path to floodspreader.py
            script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
            floodspreader_path = os.path.join(script_dir, "floodspreader.py")
            # Build the subprocess call with the full path
            call_mapper = f'python "{floodspreader_path}" {input_file}'
            subprocess.call(call_mapper, shell=True)
        elif (watershed_dict['mapper'] == "Curve2Flood" or watershed_dict['mapper'] == "FLDPLNpy"):
            LOG.info(f"Executing Curve2Flood using {input_file}")
            Curve2Flood_MainFunction(input_file, quiet=watershed_dict['quiet'])

def run_user_floodmaps(folder: FloodFolder, watershed_dict: dict, timer: Timer):
    for floodmap, input_file in zip(folder.User_Flood_Maps, folder.Model_Input_Files):
        if not watershed_dict.get('overwrite_floodmaps', True) and os.path.exists(floodmap):
            LOG.info(f"{floodmap} exists and we aren't overwriting it...")
            continue
    
        LOG.info('Creating User Flood Event to be stored here: ' + floodmap)
        run_flood_mapper(watershed_dict, folder, timer, input_file)
        LOG.info('User Flood Raster saved here : ' + floodmap)
        LOG.info('User Flood Shapefile saved here : ' + floodmap.replace('.tif','.shp'))

def run_forecast_floodmapping(folder: FloodFolder, watershed_dict: dict, timer: Timer):
    if not watershed_dict.get('overwrite_floodmaps', True) and os.path.exists(folder.Forecast_Flood_Map):
        LOG.info(f"{folder.Forecast_Flood_Map} exists and we aren't overwriting it...")
        return
    
    # (May want to look here to do a reduced flow file to only flows that exceed a threshold.  Could also do in the forecast flow function)
    LOG.info('Creating Forecast Flood Event to be stored here: ' + folder.Forecast_Flood_Map)
    run_flood_mapper(watershed_dict, folder, timer, folder.ARC_FileName_FloodForecast)
    LOG.info('Forecast Flood Raster saved here : ' + folder.Forecast_Flood_Map)
    LOG.info('Forecast Flood Shapefile saved here : ' + folder.Forecast_Flood_Map.replace('.tif','.shp'))

def get_streamids_from_source(streamflow_source: str):
    if streamflow_source.upper().startswith("NWM"):
        return 'COMID', 'TOCOMID'
    
    if streamflow_source.upper() == "GEOGLOWS":
        return 'LINKNO', 'DSLINKNO'
    
    LOG.error(f"streamflow_source {streamflow_source} not recognized, please use either 'NWM' or 'GEOGLOWS'")
    raise ValueError(f"streamflow_source {streamflow_source} not recognized, please use either 'NWM' or 'GEOGLOWS'")

def run_go_consequences(folder: FloodFolder, timer: Timer):
    LOG.info("Creating the Go-Consequences JSON file and running the Go-Consequences Docker container...")

    with timer('go_consequences'):
        for depth_file in folder.get_depth_files():
            Forecast_Flood_Depth_Raster_Name = os.path.basename(depth_file)
            Consequences_JSON_File = Forecast_Flood_Depth_Raster_Name.replace('.tif','_consequences.json') 
            Consequences_JSON_Path = os.path.join(folder.Consequences_Folder, Consequences_JSON_File)
            Consequences_Output_GPKG_File = Consequences_JSON_File.replace('.json','.gpkg')
            LOG.info(f"Creating consequences file {Consequences_JSON_Path}")
            Create_Go_Consequence_GeoJSON(Consequences_JSON_Path, Forecast_Flood_Depth_Raster_Name, Consequences_Output_GPKG_File)
            # run the go-consequences Docker container
            source_dir = os.path.join(folder.output_dir, folder.watershed)
            # docker run --rm --mount type=bind,source="D:\nencarta",target=/data go-consequences /data/results/streams_715_WseBanks_NotClean_Curve2Flood/consequences/USGS_13_n37w106_ARC_FloodDepth_Forecast_20250807_consequences.json
            docker_command = f'docker run --rm --mount type=bind,source="{source_dir}",target=/data go-consequences:latest /data/Consequences/{Consequences_JSON_File}'
            # run the docker command
            subprocess.call(docker_command, shell=True)


def run_one_dem(DEM: str, folder: FloodFolder, watershed_dict: dict, timer: Timer):
    if not (DEM.endswith(".tif") or DEM.endswith(".img")):
        return
        
    folder.setup_folder_for_dem(DEM, watershed_dict)
    folder.set_source_landcover_files(ESA.download_and_process_land_cover(folder))

    # This function sets-up the Input files for ARC and FloodSpreader
    # It also does some of the geospatial processing
    Process_Geospatial_Data(folder, watershed_dict, DEM) 

    # if the DEM_StrmShp file is empty, then we can't do anything
    if not folder.DEM_StrmShp:
        LOG.info(f"Results for {DEM} are not possible because we don't have a stream shapefile...")
        return 
    
    # read in the reanalysis streamflow and break the code if the dataframe is empty or if the streamflow is all 0
    DEM_Reanalsyis_FlowFile_df = pd.read_csv(folder.DEM_Reanalsyis_FlowFile, usecols=[watershed_dict['specified_highflow_field']])
    if DEM_Reanalsyis_FlowFile_df.empty or DEM_Reanalsyis_FlowFile_df[watershed_dict['specified_highflow_field']].values.mean() <= 0 or len(DEM_Reanalsyis_FlowFile_df.index)==0:
        LOG.info(f"Results for {DEM} are not possible because we don't have streamflow estimates...")
        return

    remove_old_forecast_files(folder, watershed_dict)

    # # creat the initial flood map with the stream raster and land cover data
    if not watershed_dict['use_specified_depth_for_bathy_mask'] or watershed_dict['clean_dem']:
        Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(folder, watershed_dict['land_watervalue'])
    
        run_dem_cleaner(folder, watershed_dict, timer, DEM)
        create_bathymetry(folder, watershed_dict, timer)

    if watershed_dict['floodmap_mode'] == 'forecast':
        run_forecast_floodmapping(folder, watershed_dict, timer)
    elif watershed_dict['floodmap_mode'] == 'user':
        run_user_floodmaps(folder, watershed_dict, timer)
    if watershed_dict['make_fist_inputs']:
        create_fist_inputs(folder, watershed_dict, timer)
    if watershed_dict['estimate_consequences']:
        run_go_consequences(folder, timer)

    return

def process_river_geometry(folder: FloodFolder, watershed_dict: dict, DEM: str):
    if not watershed_dict['process_stream_network']:
        return None
    
    #Datasets that can be good for a large domain
    StrmSHP: str = watershed_dict['flowline']
    LOG.info('Reading in stream file: ' + StrmSHP)
    if StrmSHP.endswith(".gdb"):
        # Specify the layer you want to access
        layer_name = "geoglowsv2"
        # Read the layer from the geodatabase
        StrmShp_gdf = gpd.read_file(StrmSHP, layer=layer_name, use_arrow=True)    
    elif StrmSHP.endswith((".shp", ".gpkg")):
        # Read the layer from the shapefile
        StrmShp_gdf = gpd.read_file(StrmSHP, use_arrow=True)
    elif StrmSHP.endswith(".parquet"):
        # Read the layer from the shapefile
        StrmShp_gdf = gpd.read_parquet(StrmSHP)
    else:
        raise ValueError("Unsupported stream file format. Please provide a .gdb, .shp, .gpkg, or .parquet file.")

    LOG.info('Converting the coordinate system of the stream file to match the DEM files, if necessary')
    test_dem_path = os.path.join(folder.dem_folder, DEM)
    # Load the DEM file and get its CRS using gdal
    dem_dataset: gdal.Dataset = gdal.Open(test_dem_path)
    dem_crs = CRS.from_wkt(dem_dataset.GetProjection())
    strm_crs = CRS.from_user_input(StrmShp_gdf.crs)
    has_different_crs = dem_crs != strm_crs

    # get bounding box of the DEM
    gt = dem_dataset.GetGeoTransform()
    minx = gt[0]
    maxx = gt[0] + (gt[1] * dem_dataset.RasterXSize)
    miny = gt[3] + (gt[5] * dem_dataset.RasterYSize)
    maxy = gt[3]

    if has_different_crs:
        minx, miny, maxx, maxy = gpd.GeoSeries(box(minx, miny, maxx, maxy), crs=dem_crs).to_crs(strm_crs).total_bounds

    # Filter to area of interest; avoids heavy memory use and is faster
    StrmShp_gdf = StrmShp_gdf.cx[minx:maxx, miny:maxy]

    # Check if the CRS of the shapefile matches the DEM's CRS
    if has_different_crs:
        LOG.info("DEM and Stream Network have different coordinate systems...")
        LOG.info(f"Stream CRS: {strm_crs.to_string()}")
        LOG.info(f"DEM CRS: {dem_crs.to_string()}")
        # Reproject the shapefile to match the DEM's CRS
        StrmShp_gdf = StrmShp_gdf.to_crs(dem_crs)

    # removing any lingering NoneType geometries
    StrmShp_gdf = StrmShp_gdf[~StrmShp_gdf.geometry.isna()]

    return StrmShp_gdf

def remove_landcover_tiles(folder: FloodFolder):
    # delete the ESA_LC_Folder and the data in it
    # Loop through all files in the directory and remove them
    for file in glob.glob(os.path.join(folder.ESA_LC_Folder, '*')):
        try:
            if os.path.isfile(file):
                # Adjust file permissions before deletion
                if platform.system() == "Windows":
                    os.chmod(file, stat.S_IWRITE)  # Remove read-only attribute on Windows
                else:
                    os.chmod(file, stat.S_IWUSR)   # Give user write permission on Unix systems
                os.remove(file)
                LOG.info(f"remove_landcover_tiles: Deleted file: {file}")
        except Exception as e:
            LOG.error(f"Error deleting file {file}: {e}")

    if os.path.exists(folder.ESA_LC_Folder):
        # Adjust file permissions before deletion
        if platform.system() == "Windows":
            os.chmod(folder.ESA_LC_Folder, stat.S_IWRITE)  # Remove read-only attribute on Windows
        else:
            os.chmod(folder.ESA_LC_Folder, stat.S_IWUSR)   # Give user write permission on Unix systems
        os.rmdir(folder.ESA_LC_Folder)
        LOG.info(f"remove_landcover_tiles: Deleted empty folder: {folder.ESA_LC_Folder}")
    else:
        LOG.info(f"remove_landcover_tiles: Folder {folder.ESA_LC_Folder} does not exist.")

def simulation_times_to_strings(watershed_name: str, timer: Timer) -> list[str]:
    times = [
        f"Here are the simulation times for each of the processes for the watershed {watershed_name}:\n",
        f"ARC Initial Flood Simulation Time: {timer.get_time_string('arc_initial')}",
        f"Initial Flood Simulation Time: {timer.get_time_string('initial_flood_for_cleaner')}",
        f"DEM Cleaner Simulation Time: {timer.get_time_string('dem_cleaner')}",
        f"ARC Bathymetry Simulation Time: {timer.get_time_string('arc_bathy')}",
        f"Flooder Bathymetry Simulation Time: {timer.get_time_string('flood_bathy')}",
        f"Flood Map Simulation Time: {timer.get_time_string('flood')}",
        f"GeoJSON Simulation Time: {timer.get_time_string('geojson_fist')}",
        f"Go-Consequences Simulation Time: {timer.get_time_string('go_consequences')}"
    ]
    return times

def print_simulation_times(watershed_name: str, timer: Timer):
    for time_string in simulation_times_to_strings(watershed_name, timer):
        LOG.info(time_string)

def process_dem(watershed_dict: dict, timer: Timer):
    LOG.info(f"Estimate consequences is set to {watershed_dict['estimate_consequences']}")

    # API key for NWM requests (required when streamflow_source is NWM)
    if watershed_dict['streamflow_source'].upper().startswith("NWM") and not watershed_dict.get('nwm_api_key'):
        raise ValueError("nwm_api_key is required when streamflow_source is NWM.")

    # Folder Management
    folder = FloodFolder(watershed_dict)

    # Validate data
    if watershed_dict.get('mapper') not in ["FloodSpreader", "Curve2Flood", "FLDPLNpy"]:
        raise ValueError("Invalid mapper specified. Choose 'FloodSpreader', 'Curve2Flood', or 'FLDPLNpy'.")
    
    if watershed_dict.get('mapper') == 'FLDPLNpy':
        if watershed_dict.get('move_stream_network_to_new_locations') is False:
            raise ValueError("The stream network needs to be moved to use the FLDPLNpy mapper. " \
            "Please set move_stream_network_to_new_locations to true in order to proceed...")
    
    if not watershed_dict.get('mannings_text_file'):
        Create_BaseLine_Manning_n_File_ESA(folder.mannings_n_text_file)

    # If you are using the warning flags to download the DEM data, do it now
    if watershed_dict['use_warning_flags_to_download_dem']:
        # This outputs a list of DEMs were GEOGLOWS has forecasted flooding (2-year exceedance or above)
        DEM_List = ForecastFlows.Download_USGS_DEM_Data_Using_WarningFlag_Data(
            watershed_dict['geoglows_vpu'], folder.dem_folder, watershed_dict['forensic_forecast_date'])
    else:
        #This is the list of all the DEM files we will go through
        filter = watershed_dict.get('dem_filter', '*')
        DEM_List = [os.path.basename(f) for f in glob.glob(os.path.join(folder.dem_folder, filter))]
    
    # if DEM List is empty, then we need to break out of the function
    if len(DEM_List) == 0:
        LOG.info("No DEMs found in the specified folder.")
        return

    #Now go through each DEM dataset
    for DEM in DEM_List:
        run_one_dem(DEM, folder, watershed_dict, timer)
    
    remove_landcover_tiles(folder)
    print_simulation_times(watershed_dict['name'], timer)
    return
    
def process_json_input_serial(json_file):
    """Process input from a JSON file."""
    with open(json_file, 'r') as file:
        LOG.info(f'Opening JSON file: {json_file}')
        data: dict = json.load(file)
        LOG.info(f"{os.linesep}{pprint.pformat(data)}")
    
    watersheds: list[dict] = data.get("watersheds", [])
    timer = Timer()
    for watershed in watersheds:
        process_watershed(watershed, timer)

    return timer

def process_single_watershed(watershed: dict):
    """Run a single watershed processing job and log to a file."""
    watershed_name = watershed.get("name", "unnamed")
    safe_name = watershed_name.replace(" ", "_").replace("/", "_")
    log_dir = os.path.join("logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{safe_name}.log")

    with open(log_path, 'w') as log_file, redirect_stdout(log_file), redirect_stderr(log_file):
        try:
            timer = Timer()
            process_watershed(watershed, timer)
            return timer
        except Exception as e:
            LOG.error(f"Exception during processing {watershed_name}: {e}")

def validate_forecast_hour(forensic_forecast_hour):
    if forensic_forecast_hour:
        try:
            forensic_forecast_hour = int(forensic_forecast_hour)
            if forensic_forecast_hour not in range(0, 24):
                raise ValueError("forensic_forecast_hour must be between 0 and 23")
            forensic_forecast_hour = f"{forensic_forecast_hour:02d}"
        except ValueError:
            raise ValueError(f"Invalid forensic_forecast_hour: {forensic_forecast_hour}")
            
    return forensic_forecast_hour

def validate_forecast_hours(streamflow_source: str, forensic_forecast_hour: str, watershed_name: str):
    # if the streamflow_source is "NWM_short_range" the forensic_forecast_hour can be between 0 and 23, the forensic_forecast_hour must be provided as a two-digit string
    short_range_forecast_hours = [f"{i:02d}" for i in range(0, 24)]
    medium_range_forecast_hours = ["00", "06", "12", "18"]
    long_range_forecast_hours = ["00"]
    if streamflow_source == "NWM_short_range" and (forensic_forecast_hour and forensic_forecast_hour not in short_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be between 0 and 23 when 'streamflow_source' is 'NWM_short_range'.")
    # if the streamflow_source is "NWM_medium_range" the forensic_forecast_hour can be between one of 0, 6, 12, or 18
    if streamflow_source == "NWM_medium_range" and (forensic_forecast_hour and forensic_forecast_hour not in medium_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be one of 0, 6, 12, or 18 when 'streamflow_source' is 'NWM_medium_range'.")
    # if the streamflow_source is "NWM_long_range" the forensic_forecast_hour can be only 0
    if streamflow_source == "NWM_long_range" and (forensic_forecast_hour and forensic_forecast_hour not in long_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be 0 when 'streamflow_source' is 'NWM_long_range'.")

def validate_nwm_api_key(nwm_api_key: str, watershed_name: str, streamflow_source: str):
     if streamflow_source.upper().startswith("NWM") and not nwm_api_key:
        raise ValueError(f"Watershed '{watershed_name}' requires 'nwm_api_key' when 'streamflow_source' is NWM.")

def validate_specified_depths(use_specified_depth_for_bathy_mask: bool, 
                              specify_depths_for_bathy_mask: list, 
                              clean_dem: bool, 
                              watershed_name: str):
    if use_specified_depth_for_bathy_mask:
        if not specify_depths_for_bathy_mask or not isinstance(specify_depths_for_bathy_mask, list) or len(specify_depths_for_bathy_mask) < 1:
            raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of two floats when 'use_specified_depth_for_bathy_mask' is True.")
        elif len(specify_depths_for_bathy_mask) < 2 and clean_dem:
            raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of two floats when 'clean_dem' is True.")
        elif len(specify_depths_for_bathy_mask) > 1 and not clean_dem:
            raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of one float when 'clean_dem' is False.")
        
def validate_forecast_date(forensic_forecast_date: str, streamflow_source: str):
    # check if forensic_forecast_date and forensic_forecast_hour is provided in the watershed dictionary and if not set forensic_forecast_date=None
    # get forensic forecast date (string like "20231125" or "2023-11-25 06:00:00 UTC")
    if forensic_forecast_date:
        try:
            # First try YYYYMMDD format
            forensic_forecast_date_dt = datetime.strptime(forensic_forecast_date, '%Y%m%d')
        except ValueError:
            try:
                # Fallback for full timestamp
                forensic_forecast_date_dt = datetime.strptime(forensic_forecast_date, '%Y-%m-%d %H:%M:%S %Z')
            except ValueError:
                raise ValueError(f"Invalid forensic_forecast_date format: {forensic_forecast_date}")
        
        # sanity check (only if you want to enforce July 1, 2024 rule)
        LOG.info(f"The forensic forecast date provided is {forensic_forecast_date_dt}")
        if forensic_forecast_date_dt < datetime(2024, 7, 1) and streamflow_source.upper().startswith("GEOGLOWS"):
            LOG.error(f"Warning: Forensic forecast date {forensic_forecast_date} is earlier than July 1, 2024. Exiting...")
            raise ValueError(f"Forensic forecast date {forensic_forecast_date} is earlier than July 1, 2024. Please provide a date on or after July 1, 2024.")
    else:
        forensic_forecast_date = None
        LOG.info("Forensic forecast date not provided; defaulting to None.")

    return forensic_forecast_date

def verify_required_keys(input_dict: dict):
    required_keys = ["name", "flowline", "dem_dir", "output_dir"]
    for key in required_keys:
        if key not in input_dict:
            raise KeyError(f"Missing required key in watershed {input_dict.get('name', 'unknown')}: {key}")

def validate_user_floodmaps(watershed_dict: dict):
    floodmap_mode = watershed_dict.get("floodmap_mode", "forecast")
    if floodmap_mode not in ["forecast", "user"]:
        raise ValueError(f"Watershed '{watershed_dict.get('name', 'unknown')}' has invalid 'floodmap_mode': {floodmap_mode}. Must be either 'forecast' or 'user'.")

    user_flow_files = watershed_dict.get("user_flow_files", None)
    if isinstance(user_flow_files, str):
        user_flow_files = [user_flow_files]
        
    if floodmap_mode == "user" and (not user_flow_files or not isinstance(user_flow_files, list) or len(user_flow_files) < 1):
        raise ValueError(f"Watershed '{watershed_dict.get('name', 'unknown')}' requires 'user_flow_files' as either a filepath string or a list of file paths when 'floodmap_mode' is 'user'.")
    

    return floodmap_mode, [os.path.normpath(f) for f in user_flow_files]

def norm_or_none(path: str):
    return os.path.normpath(path) if path else None

def float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid q_baseflow_threshold: {value}") from exc

def process_watershed(input_dict: dict, timer: Timer = None):
    """The core logic for processing a watershed."""
    verify_required_keys(input_dict)
    watershed_name = input_dict.get("name")
    
    streamflow_source = input_dict.get("streamflow_source", "GEOGLOWS")
    forensic_forecast_hour = validate_forecast_hour(input_dict.get("forensic_forecast_hour"))
    validate_forecast_hours(streamflow_source, forensic_forecast_hour, watershed_name)

    nwm_api_key = input_dict.get("nwm_api_key")
    validate_nwm_api_key(nwm_api_key, watershed_name, streamflow_source)

    use_specified_depth_for_bathy_mask = input_dict.get("use_specified_depth_for_bathy_mask", True)
    specify_depths_for_bathy_mask = input_dict.get("specify_depths_for_bathy_mask")
    clean_dem = input_dict.get("clean_dem", False)
    validate_specified_depths(use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask, clean_dem, watershed_name)

    floodmap_mode, user_flow_files = validate_user_floodmaps(input_dict)

    if not input_dict.get("make_depth_maps", True) and input_dict.get('estimate_consequences', False):
        LOG.warning(f"Watershed '{watershed_name}': 'make_depth_maps' is False but 'estimate_consequences' is True. Setting 'make_depth_maps' to True.")
        input_dict['make_depth_maps'] = True

    dem_filter = input_dict.get("dem_filter", "*")
    if not dem_filter:
        dem_filter = "*"

    move_stream_network_to_new_locations = input_dict.get("move_stream_network_to_new_locations", False)
    stream_order_threshold = float_or_none(input_dict.get("new_strm_threshold_km2"))
    if move_stream_network_to_new_locations is True and stream_order_threshold is None:
        raise ValueError(f"Watershed '{watershed_name}': 'stream_order_threshold' must be specified when moving stream network.")

    watershed_dict = {
        "name": watershed_name,
        "flowline": os.path.normpath(input_dict["flowline"]),
        "dem_dir": os.path.normpath(input_dict["dem_dir"]),
        "output_dir": os.path.normpath(input_dict["output_dir"]),
        "bathy_use_banks": input_dict.get("bathy_use_banks", False),
        "flood_waterlc_and_strm_cells": input_dict.get("flood_waterlc_and_strm_cells", False),
        "land_watervalue": input_dict.get("land_watervalue", 80),
        "clean_dem": clean_dem,
        "mapper": input_dict.get("mapper", "FloodSpreader"),
        "process_stream_network": input_dict.get("process_stream_network", False),
        "use_specified_depth_for_bathy_mask": use_specified_depth_for_bathy_mask,
        "age_of_forecast_days": input_dict.get("age_of_forecast_days", 7),
        "find_banks_based_on_landcover": input_dict.get("find_banks_based_on_landcover", True),
        "specify_depths_for_bathy_mask": specify_depths_for_bathy_mask,
        "create_reach_average_curve_file": input_dict.get("create_reach_average_curve_file", False),
        "use_warning_flags_to_download_dem": input_dict.get("use_warning_flags_to_download_dem", False),
        "geoglows_vpu": input_dict.get("geoglows_vpu"),
        "forensic_forecast_date": validate_forecast_date(input_dict.get("forensic_forecast_date"), streamflow_source),
        "forensic_forecast_hour": forensic_forecast_hour,
        "specified_bathyflow_field":input_dict.get("specified_bathyflow_field", "p_exceed_50"),
        "specified_highflow_field":input_dict.get("specified_highflow_field", "rp100_premium"),
        "StrmOrder_Lower": input_dict.get("StrmOrder_Lower"),
        "StrmOrder_Upper": input_dict.get("StrmOrder_Upper"),
        "q_baseflow_threshold": float_or_none(input_dict.get("q_baseflow_threshold")),
        "lake_filter_json": norm_or_none(input_dict.get("lake_filter_json")),
        "estimate_consequences": input_dict.get("estimate_consequences", False),
        "streamflow_source": streamflow_source,
        "nwm_api_key": nwm_api_key,
        "overwrite_floodmaps": input_dict.get("overwrite_floodmaps", True),
        "remove_old_forecast_files": input_dict.get("remove_old_forecast_files", False),
        "make_fist_inputs": input_dict.get("make_fist_inputs", True),
        "dem_filter": dem_filter,
        "floodmap_mode": floodmap_mode,
        "user_flow_files": user_flow_files,
        "make_curvefile": input_dict.get("make_curvefile", True),
        "make_ap_database": input_dict.get("make_ap_database", True),
        "vdt_file_extension": input_dict.get("vdt_file_extension", 'txt'),
        "mannings_text_file": norm_or_none(input_dict.get("mannings_text_file")),
        "bathy_args": input_dict.get("bathy_args", {}),
        "floodmap_args": input_dict.get("floodmap_args", {}),
        "make_depth_maps": input_dict.get("make_depth_maps", True),
        "make_velocity_maps": input_dict.get("make_velocity_maps", True),
        "make_wse_maps": input_dict.get("make_wse_maps", True),
        "floodmap_identifier": input_dict.get("floodmap_identifier", ""),
        "move_stream_network_to_new_locations": input_dict.get("move_stream_network_to_new_locations", False),
        "new_strm_threshold_km2": float_or_none(input_dict.get("new_strm_threshold_km2")),
        "min_match_score": float_or_none(input_dict.get("min_match_score")),
        "quiet": input_dict.get("quiet", False),
    }

    os.makedirs(watershed_dict["output_dir"], exist_ok=True)

    LOG.info(f"Started processing watershed: {watershed_name}")
    LOG.info(f"Parameters: {pprint.pformat(watershed_dict)}")

    if timer is None:
        timer = Timer()
    process_dem(watershed_dict, timer)

    LOG.info(f"Finished processing {watershed_name}")

def process_json_input(json_file, parallel=None, num_workers=None):
    """
    Process watersheds defined in a JSON file, optionally in parallel.

    If 'parallel' / 'num_workers' are None, they are resolved from the JSON file
    via keys "parallel" (or legacy "run_parallel") and "num_workers" (or "workers").
    CLI overrides take precedence when provided.
    """
    with open(json_file, 'r') as file:
        LOG.info(f"Reading input file: {json_file}")
        data = json.load(file)

    watersheds = data.get("watersheds", [])
    if not watersheds:
        LOG.warning("No watersheds found.")
        return

    # Resolve final parallel settings (JSON + optional CLI override)
    parallel, num_workers = _resolve_parallel_settings(
        data,
        cli_parallel=parallel,
        cli_num_workers=num_workers
    )
    LOG.info(f"Parallel: {parallel} | Workers: {num_workers}")
    if parallel and len(watersheds) > 1:
        # Parallel path: one process per watershed, logging handled by process_single_watershed
        with mp.Pool(processes=num_workers) as pool:
            pool.imap_unordered(process_single_watershed, watersheds)
    else:
        # Serial path: reuse existing serial routine (retains your validation flow)
        process_json_input_serial(json_file)

def rename_cli_keys(input_dict: dict) -> dict:
    """Rename CLI argument keys to match watershed dictionary keys."""
    key_mapping = {
        "watershed":"name",
    }
    return {key_mapping.get(key, key): value for key, value in input_dict.items()}

def process_cli_arguments(args):
    """Process input from CLI arguments."""
    input_dict = vars(args)
    input_dict = rename_cli_keys(input_dict)
    process_watershed(input_dict)

class RequiredIfFloodWaterAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)

        if namespace.flood_waterlc_and_strm_cells and values is None:
            parser.error("--land_watervalue is required when --flood_waterlc_and_strm_cells is set to True.")


def main():
    parser = argparse.ArgumentParser(description="Flood Mapping Script")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand for JSON input
    json_parser = subparsers.add_parser("json", help="Process watersheds from a JSON file")
    json_parser.add_argument("json_file", type=str, help="Path to the JSON file")
    # Let CLI override JSON for parallel choice
    grp = json_parser.add_mutually_exclusive_group()
    grp.add_argument("--parallel", dest="parallel", action="store_const", const=True, default=None,
                     help="Force parallel processing (overrides JSON).")
    grp.add_argument("--serial", dest="parallel", action="store_const", const=False,
                     help="Force serial processing (overrides JSON).")
    json_parser.add_argument("--num_workers", type=int, default=None,
                             help="Number of workers when running in parallel (overrides JSON).")

    # # Subcommand for JSON input
    # json_parser = subparsers.add_parser("json", help="Process watersheds from a JSON file")
    # json_parser.add_argument("json_file", type=str, help="Path to the JSON file")

    # Subcommand for CLI input
    cli_parser = subparsers.add_parser("cli", help="Process watershed parameters via CLI")
    cli_parser.add_argument("watershed", type=str, help="Watershed name")
    cli_parser.add_argument("flowline", type=str, help="Path to the flowline shapefile")
    cli_parser.add_argument("dem_dir", type=str, help="Directory containing DEM files")
    cli_parser.add_argument("output_dir", type=str, help="Directory where results will be saved")
    cli_parser.add_argument("--bathy_use_banks", action="store_true", help="Use bathy banks for processing")
    cli_parser.add_argument("--flood_waterlc_and_strm_cells", action="store_true",
                        help="In the flood inundation maps it shows water related land use and stream raster cells as flooded")
    cli_parser.add_argument("--land_watervalue", type=int, action=RequiredIfFloodWaterAction,
                        help="Land water value in the land cover raster (Required if --flood_waterlc_and_strm_cells is True)")
    cli_parser.add_argument("--clean_dem", action="store_true", help="Clean DEM data before processing")
    cli_parser.add_argument("--process_stream_network", action="store_true", help="Clean DEM data before processing")
    cli_parser.add_argument("--mapper", type=str, default="Curve2Flood", choices=["FloodSpreader", "Curve2Flood", "FLDPLNpy"], help="Mapping method")
    cli_parser.add_argument("--use_specified_depth_for_bathy_mask", action="store_true", help="Specify a depth for FloodSprederPy to use for bathymetry masking")
    cli_parser.add_argument("--age_of_forecast_days", type=int, default=7, help="Age of forecast in days")
    cli_parser.add_argument("--find_banks_based_on_landcover", action="store_true", help="Use landcover data for finding banks when estimating bathymetry")
    cli_parser.add_argument("--specify_depths_for_bathy_mask", type=float, nargs=2, help="Specify two floats for bathymetry depth mask when '--use_specified_depth_for_bathy_mask' is True")
    cli_parser.add_argument("--create_reach_average_curve_file", action="store_true", help="Create a reach average curve file instead of one that varies for each stream cell")
    cli_parser.add_argument("--forensic_forecast_date", type=str, default=None, help="Forensic forecast date in YYYYMMDD format (defaults to most recent forecast) unless this argument is provided")
    cli_parser.add_argument("--forensic_forecast_hour", type=int, default=0, choices=[i for i in range (0,24)], help="Forensic forecast hour (defaults to 0) unless this argument is provided")
    cli_parser.add_argument("--specified_bathyflow_field", type=str, default="p_exceed_50", help="Specify the streamflow field in the streamflow reanalysis file that will be used for bathymetry estimation  (defaults to 'p_exceed_50') ")
    cli_parser.add_argument("--specified_highflow_field", type=str, default="rp100_premium", help="Specify the highflow field in the streamflow reanalysis file that will be used by ARC as the highest flow for the VDT database and curvefile creation (defaults to 'rp100_premium')")
    cli_parser.add_argument("--StrmOrder_Field", type=str, default=None, help="Stream order field in the stream shapefile (optional)")
    cli_parser.add_argument("--StrmOrder_Upper", type=int, default=None, help="Upper bound for stream order (optional)")
    cli_parser.add_argument("--q_baseflow_threshold", type=float, default=None, help="Drop streams whose baseflow is below this threshold (optional)")
    cli_parser.add_argument("--use_warning_flags_to_download_dem", action="store_true", help="Use warning flags to download DEM data")
    cli_parser.add_argument("--geoglows_vpu", type=int, default=None, help="GEOGLOWS VPU ID (required if --use_warning_flags_to_download_dem is set to True)")
    cli_parser.add_argument("--lake_filter_json", type=str, default=None, help="Path to the lake filter JSON file (optional)")
    cli_parser.add_argument("--estimate_consequences", action="store_true", help="Estimate consequences using go-consequences")
    cli_parser.add_argument("--streamflow_source", type=str, default="GEOGLOWS", choices=["NWM", "GEOGLOWS"], help="Streamflow source for NenCarta (defaults to GEOGLOWS)")
    cli_parser.add_argument("--nwm_api_key", type=str, default=None, help="NWM API key (required when --streamflow_source is NWM)")
    cli_parser.add_argument("--overwrite_floodmaps", action="store_true", help="Overwrite existing forecast flood maps")
    cli_parser.add_argument("--remove_old_forecast_files", action="store_true", help="Remove old forecast files before processing")
    cli_parser.add_argument("--make_fist_inputs", action="store_true", help="Make FIST inputs after processing")
    cli_parser.add_argument("--move_stream_network_to_new_locations", action="store_true", help="Move stream network to new locations")
    cli_parser.add_argument("--new_strm_threshold_km2", type=float, default=None, help="The stream threshold for creating a new stream network for the DEM that you will be using. Use in conjunction with move_stream_network_to_new_locations and FLDPLNpy")
    cli_parser.add_argument("--min_match_score", type=float, default=None, help="The score needed to conflate the new DEM based network with the one provided by the as the `flowline` input")
    gui_parser = subparsers.add_parser("gui", help="Summon the GUI application")

    args = parser.parse_args()

    

    if args.command == "json":
        LOG.info('Processing ' + str(args.json_file))
        process_json_input(args.json_file, parallel=args.parallel, num_workers=args.num_workers)
    elif args.command == "cli":
        process_cli_arguments(args)
    elif args.command == "gui":
        gui_app.run_gui()


if __name__ == "__main__":
    main()
