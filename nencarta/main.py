#conda env create -f **.yml
#conda activate ffs_esa_dc

# build-in imports
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import glob
import multiprocessing as mp
import sys
import os
import platform
import re
import stat
import subprocess
import pprint
from typing import TextIO

# third-party imports
from arc import Arc
from arc.Create_GeoJSON import Run_Main_VDT_to_GEOJSON_Program_Stream_Vector
from curve2flood import Curve2Flood_MainFunction
from osgeo import gdal, osr
from shapely.geometry import box
import json
import numpy as np
import pandas as pd
import geopandas as gpd
import numpy as np

# local imports
from . import streamflow_processing as HistFlows
from . import Download_Process_ForecastData as ForecastFlows
from . import DEM_Cleaner
from . import esa_download_processing as ESA
from . import gui_app
from . import Hydroterrain_Processing
from . import LOG
from .flood_folder import FloodFolder
from .timer import Timer


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

def get_rivids(folder: FloodFolder, watershed_dict: dict, DEM: str, stream_id_field: str) -> np.ndarray | None:
    # now we need to figure out if our DEM_StrmShp and DEM_Reanalysis_Flowfile exists and if not, create it
    if os.path.isfile(folder.DEM_StrmShp) and os.path.isfile(folder.DEM_Reanalsyis_FlowFile):
        LOG.info(folder.DEM_StrmShp + ' Already Exists')
        LOG.info(folder.DEM_Reanalsyis_FlowFile + ' Already Exists')
        rivids = gpd.read_file(folder.DEM_StrmShp, columns=[stream_id_field], use_arrow=True).values
    else:
        # Before we get too far ahead, let's make sure that our DEM and Flowlines have the same coordinate system
        StrmShp_gdf = process_river_geometry(folder, watershed_dict, DEM) 
        rivids, _ = HistFlows.Process_and_Write_Retrospective_Data_for_DEM_Tile(StrmShp_gdf, stream_id_field, folder, watershed_dict)

    if rivids is None or len(rivids) == 0:
        LOG.info('DEM_StrmShp is empty, returning None values')
        return None

    return rivids



def Process_FloodForecasting_Geospatial_Data(folder: FloodFolder, watershed_dict: dict, DEM: str):
    #Get the Spatial Information from the DEM Raster
    (minx, miny, maxx, maxy, dx, dy, ncols, nrows, _, dem_projection) = Get_Raster_Details(folder.DEM_File)
    projWin_extents = [minx, maxy, maxx, miny]
    outputBounds = [minx, miny, maxx, maxy]  #https://gdal.org/api/python/osgeo.gdal.html
   
    #Create Land Dataset
    if os.path.isfile(folder.LAND_File):
        LOG.info(folder.LAND_File + ' Already Exists')
    else: 
        LOG.info('Creating ' + folder.LAND_File) 
        Create_AR_LandRaster(folder.LandCoverFiles, folder.LAND_File, projWin_extents, dem_projection, ncols, nrows)

    streamflow_source = watershed_dict['streamflow_source']
    # set the ID used for the stream network
    if streamflow_source.upper().startswith("NWM"):
        stream_id_field = 'COMID'
    elif streamflow_source.upper() == "GEOGLOWS":
        stream_id_field = 'LINKNO'
    else:
        LOG.error(f"streamflow_source {streamflow_source} not recognized, please use either 'NWM' or 'GEOGLOWS'")
        sys.exit()

    rivids = get_rivids(folder, watershed_dict, DEM, stream_id_field)
    if rivids is None:
        return

    if os.path.isfile(folder.STRM_File):
        LOG.info(folder.STRM_File + ' Already Exists')
    else:
        LOG.info('Creating ' + folder.STRM_File)
        Create_AR_StrmRaster(folder.DEM_StrmShp, folder.STRM_File, outputBounds, minx, miny, maxx, maxy, dx, dy, ncols, nrows, 'LINKNO')
    
    if os.path.isfile(folder.STRM_File_Clean):
        LOG.info(folder.STRM_File_Clean + ' Already Exists')
    else:
        LOG.info('Creating ' + folder.STRM_File_Clean)
        Clean_STRM_Raster(folder.STRM_File, folder.STRM_File_Clean)

    # # if the forensic forecast hour is provided, use it to set the forecastdate_formatted
    # if forensic_forecast_hour is not None:
    #     forecasthour = forensic_forecast_hour
    # # if no forensic forecast hour is provided, use the one closest to the current hour for each NWM foreacst type
    # # the short range should have a forecast every hour
    # elif forecast_type == 'short_range' and forensic_forecast_hour is None:
    #     forecasthour = datetime.utcnow().strftime('%H')
    # # the medium range should have a forecast every 6 hours
    # elif forecast_type == 'medium_range' and forensic_forecast_hour is None:
    #     current_hour = int(datetime.utcnow().strftime('%H'))
    #     forecasthour = str((current_hour // 6) * 6).zfill(2)
    # # the long range should have a forecast every 24 hours  
    # elif forecast_type == 'long_range' and forensic_forecast_hour is None:
    #     forecasthour = '00'

    
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
                    # we only need the forecast date for GEOGLOWS, for NWM we need the forecast hour as well               
                    if streamflow_source.upper() == "GEOGLOWS":
                        ForecastFlowFile = os.path.join(folder.FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{streamflow_source}_forecast.csv')
                    elif streamflow_source.upper().startswith("NWM"):
                        ForecastFlowFile = os.path.join(folder.FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{forecasthour}_{streamflow_source}_forecast.csv')
                    if not os.path.exists(ForecastFlowFile):
                        ForecastFlows.Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, ForecastFlowFile, streamflow_source, watershed_dict['nwm_api_key'])
                    found = True
                    break
                except Exception as e:
                    LOG.error(f'Could not process forecast, moving back another day.. ({e})')
            if found:
                break  # break outer
    LOG.info('Forecast data save here: ' + ForecastFlowFile)
    
    # if the mapper is "FLDPLN", we need to create a flow direction raster using the bathymetry based DEM.
    if watershed_dict['mapper'] == "FLDPLN":
        folder.setup_fldpln_files()
        if os.path.exists(folder.flowdir_orig):
            LOG.info("The flow direction raster already exists and will not be recreated...")
        else:
            Hydroterrain_Processing.create_flow_direction_raster(folder.DEM_File, folder.Flow_Direction_Folder, folder.flowdir_orig)
    
    #Create a Starting AutoRoute Input File
    LOG.info('Creating ARC Input File: ' + folder.ARC_FileName_Initial)
    #Create the Initial Flow
    LOG.info(f"Using the field '{watershed_dict['specified_bathyflow_field']}' for bathymetry estimation and '{watershed_dict['specified_highflow_field']}' for flood mapping...\n")

    Create_FlowFile(folder.DEM_Reanalsyis_FlowFile, folder.COMID_Q_File, 'COMID', 'rp2')

    # Create the Initial input file which is only used if cleaning the DEM
    if watershed_dict['clean_dem']: 
        Create_ARC_Model_Input_File_Initial(folder, watershed_dict, 'COMID')

    Create_ARC_Model_Input_File_Bathy(folder, watershed_dict, 'COMID')

    Forecast_Flood_Map, Forecast_Flood_Depth_Raster = Create_ARC_Model_Input_File_FloodForecast(ForecastFlowFile, folder, watershed_dict)

    folder.setup_flood_forecast_files(Forecast_Flood_Map, Forecast_Flood_Depth_Raster, ForecastFlowFile)
    
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
    out_file.write('#ARC_Inputs')
    out_file.write(f'\nDEM_File\t{folder.DEM_File_Clean if maybe_use_clean_dem else folder.DEM_File}') # use_clean_dem will auto pick the cleaned DEM if it exists, else we force using the original DEM
    out_file.write(f'\nStream_File\t{folder.STRM_File_Clean}')
    out_file.write(f'\nLU_Raster_SameRes\t{folder.LAND_File}')
    out_file.write(f'\nLU_Manning_n\t{folder.mannings_n_text_file}')
    out_file.write(f'\nFlow_File\t{folder.DEM_Reanalsyis_FlowFile}')
    out_file.write(f'\nFlow_File_ID\t{COMID_Param}')
    out_file.write(f'\nFlow_File_BF\t{watershed_dict['specified_bathyflow_field']}')
    out_file.write(f'\nFlow_File_QMax\t{watershed_dict['specified_highflow_field']}')
    out_file.write(f'\nSpatial_Units\tdeg')
    out_file.write(f'\nX_Section_Dist\t5000.0')
    out_file.write(f'\nDegree_Manip\t6.1')
    out_file.write(f'\nDegree_Interval\t1.5')
    out_file.write(f'\nLow_Spot_Range\t2')
    out_file.write(f'\nStr_Limit_Val\t1')
    out_file.write(f'\nGen_Dir_Dist\t10')
    out_file.write(f'\nGen_Slope_Dist\t10')
    out_file.write(f'\nStream_Slope_Method\tlocal_average_corrected')

def _write_fldpln_section(out_file: TextIO, folder: FloodFolder, watershed_dict: dict, use_bathy_flow_dir: bool = False):
    out_file.write('\n\n#FLDPLN_Specific_Inputs')
    out_file.write(f'\nUse_FLDPLN_Model\tTrue')
    out_file.write(f'\nFlow_Direction_File\t{folder.flowdir_bathy if use_bathy_flow_dir else folder.flowdir_orig}')
    out_file.write(f'\nStrmOrder_Field\t{watershed_dict['StrmOrder_Field']}')
    out_file.write(f'\nDownstream_Link_Field\t{watershed_dict['Downstream_Link_Field']}')
    out_file.write(f'\nFLDPLN_fldmn\t0.01')
    out_file.write(f'\nFLDPLN_fldmx\t50')
    out_file.write(f'\nFLDPLN_dh\t0.5')
    out_file.write(f'\nFLDPLN_mxht0\t0.0')
    out_file.write(f'\nFLDPLN_ssflg\t1')

def Create_ARC_Model_Input_File_Initial(folder: FloodFolder, watershed_dict: dict, COMID_Param: str):
    with open(folder.ARC_FileName_Initial, 'w') as out_file:
        _write_arc_input_section(out_file, folder, watershed_dict, COMID_Param, False)

        out_file.write('\n\n#VDT_Output_File_and_CurveFile')
        out_file.write(f'\nVDT_Database_NumIterations	30')
        out_file.write(f'\nPrint_VDT_Database	' + folder.VDT_File.replace('.txt', '_Initial.txt'))
        out_file.write(f'\nPrint_Curve_File	' + folder.Curve_File.replace('.csv', '_Initial.csv'))
        out_file.write(f'\nReach_Average_Curve_File\t{watershed_dict['create_reach_average_curve_file']}')
        
        out_file.write('\n\n#Mapper Input Data')
        out_file.write(f'\nStrmShp_File	' + folder.DEM_StrmShp)
        out_file.write(f'\nComid_Flow_File	' + folder.COMID_Q_File)
        out_file.write(f'\nFS_ADJUST_FLOW_BY_FRACTION' + '\t' +	'1.0')
        out_file.write(f'\nBathy_Use_Banks\t{watershed_dict['bathy_use_banks']}')
        if watershed_dict['flood_waterlc_and_strm_cells']:
            out_file.write(f"\nLAND_WaterValue\t{watershed_dict['land_watervalue']}")
        if watershed_dict['find_banks_based_on_landcover']:
            out_file.write(f'\nFindBanksBasedOnLandCover\t{watershed_dict['find_banks_based_on_landcover']}')
        # out_file.write(f'\nFloodLocalOnly')
        if watershed_dict['use_specified_depth_for_bathy_mask']:
            if watershed_dict['mapper'] == "FLDPLN":
                _write_fldpln_section(out_file, folder, watershed_dict)
            out_file.write(f'\nOutFLD	' + folder.FloodMapFile.replace('.tif', '_Initial.tif'))
            out_file.write(f'\nOutSHP	' + folder.FloodMapFile.replace('.tif', '_Initial.shp'))
            out_file.write('\n' + f'FloodSpreader_SpecifyDepth	{watershed_dict['specify_depths_for_bathy_mask'][0]}')    


def Create_ARC_Model_Input_File_Bathy(folder: FloodFolder, watershed_dict: dict, COMID_Param: str):
    LOG.info('Creating ARC Input File: ' + folder.ARC_FileName_Bathy)

    out_file = open(folder.ARC_FileName_Bathy,'w')
    _write_arc_input_section(out_file, folder, watershed_dict, COMID_Param, True)
    
    out_file.write('\n\n#VDT_Output_File_and_CurveFile')
    out_file.write(f'\nVDT_Database_NumIterations	30')
    out_file.write(f'\nPrint_VDT_Database\t{folder.VDT_File_Bathy}')
    out_file.write(f'\nPrint_Curve_File\t{folder.Curve_File.replace('.csv', '_Bathy.csv')}')
    out_file.write(f'\nPrint_VDT_Test_File\t{folder.VDT_Test_File.replace('.txt', '_Bathy.txt')}')
    out_file.write(f'\nPrint_AP_Database\t{folder.VDT_File.replace('VDT_', 'AP_').replace('.txt', '_Bathy.txt')}')
    out_file.write(f'\nReach_Average_Curve_File\t{watershed_dict['create_reach_average_curve_file']}')
    
    out_file.write('\n\n#Mapper Input Data')
    out_file.write(f'\nComid_Flow_File	' + folder.COMID_Q_File)
    out_file.write(f'\nStrmShp_File	' + folder.DEM_StrmShp)
    out_file.write(f'\nFS_ADJUST_FLOW_BY_FRACTION	1.0')
    # out_file.write(f'\nFloodLocalOnly')
    out_file.write(f'\nOutFLD	' + folder.FloodMapFile.replace('.tif', '_Bathy.tif'))
    out_file.write(f'\nOutSHP	' + folder.FloodMapFile.replace('.tif', '_Bathy.shp'))
    # out_file.write(f'\nOutSHP	' + FloodMapFile.replace('.tif', '_Bathy.gpkg'))
    out_file.write(f'\nTopWidthDistanceFactor' + '\t' +	'1.5')
    out_file.write(f'\nTW_MultFact\t1.5')
    out_file.write(f'\nTopWidthPlausibleLimit\t2000')
    if watershed_dict['use_specified_depth_for_bathy_mask'] is True:
        if watershed_dict['mapper'] == "FLDPLN":
            _write_fldpln_section(out_file, folder, watershed_dict, True)

        if len(watershed_dict['specify_depths_for_bathy_mask']) == 1:
            specified_depth = watershed_dict['specify_depths_for_bathy_mask'][0]
        if len(watershed_dict['specify_depths_for_bathy_mask']) == 2:
            specified_depth = watershed_dict['specify_depths_for_bathy_mask'][1]
        out_file.write('\n' + f'FloodSpreader_SpecifyDepth\t{specified_depth}')
    elif watershed_dict['use_specified_depth_for_bathy_mask'] is False:
        out_file.write(f'\nBathyWaterMask\t{folder.FloodMapFile.replace('.tif', '_Initial.tif')}')
    out_file.write('\n\n#Bathymetry_Information')
    out_file.write(f'\nBathy_Trap_H\t0.20')
    out_file.write(f'\nBathy_Use_Banks\t{watershed_dict['bathy_use_banks']}')
    if watershed_dict['flood_waterlc_and_strm_cells'] is True:
        out_file.write(f'\nFlood_WaterLC_and_STRM_Cells\tTrue')
        out_file.write(f'\nLAND_WaterValue\t{watershed_dict['land_watervalue']}')
    if watershed_dict['find_banks_based_on_landcover'] is True:
        out_file.write(f'\nFindBanksBasedOnLandCover\tTrue')
    out_file.write(f'\nAROutBATHY	' + folder.ARC_BathyFile)
    out_file.write(f'\nBATHY_Out_File	' + folder.ARC_BathyFile)
    out_file.write(f'\nFSOutBATHY	' + folder.FS_BathyFile)

    out_file.close()

def Create_ARC_Model_Input_File_FloodForecast(ForecastFlowFile: str, folder: FloodFolder, watershed_dict: dict):
    #Create the Forecast Input File
    LOG.info('Creating ARC Input File: ' + folder.ARC_FileName_FloodForecast)
    
    out_file = open(folder.ARC_FileName_FloodForecast, 'w')
    out_file.write('#ARC_Inputs')
    out_file.write(f'\nDEM_File	' + folder.FS_BathyFile)
    out_file.write(f'\nStream_File	' + folder.STRM_File_Clean)
    
    out_file.write('\n\n#VDT_Output_File_and_CurveFile')
    out_file.write(f'\nPrint_VDT_Database	' + folder.VDT_File_Bathy)
    out_file.write(f'\nPrint_Curve_File	' + folder.Curve_File.replace('.csv', '_Bathy.csv'))
    
    out_file.write('\n\n#Mapper Input Data')
    out_file.write(f'\nStrmShp_File	' + folder.DEM_StrmShp)
    out_file.write(f'\nComid_Flow_File	' + ForecastFlowFile)
    if watershed_dict['mapper'] == "FLDPLN":
            _write_fldpln_section(out_file, folder, watershed_dict, True)
    # out_file.write(f'\nComid_Flow_File	' + r"C:\Projects\2023_MultiModelFloodMapping\Yellowstone_HydroDEM\Yellowstone_flood_2022_max_streamflow_estimate.csv")
    out_file.write(f'\nFS_ADJUST_FLOW_BY_FRACTION\t1.0')
    out_file.write(f'\nTW_MultFact\t1.5')
    out_file.write(f'\nTopWidthPlausibleLimit\t6000')
    #out_file.write(f'\nFloodLocalOnly')
    if watershed_dict['streamflow_source'].upper().startswith("NWM"):
        # create the end of the file name that describes the forecast
        ending_of_forecast_file = '_Forecast_' + str(watershed_dict['forensic_forecast_date']) + '_' + str(watershed_dict['forecasthour']) + '.tif' 
        # rename the forecast of the extent raster based upon the type of NWM forecast we are using
        Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('.tif', ending_of_forecast_file)
        Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('NWM', watershed_dict['streamflow_source'])
    else:
        # create the end of the file name that describes the forecast
        ending_of_forecast_file = '_Forecast_' + str(watershed_dict['forensic_forecast_date']) + '.tif' 
        Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('.tif', ending_of_forecast_file)

    Forecast_Flood_Depth_Raster = folder.FloodDepthFile.replace('.tif', ending_of_forecast_file)
    Forecast_Flood_WSE_Raster = folder.FloodWSEFile.replace('.tif', ending_of_forecast_file)
    Forecast_Flood_VEL_Raster = folder.FloodVELFile.replace('.tif', ending_of_forecast_file)
    Forecast_Flood_Map_Raster = folder.FloodMapFile.replace('.tif', ending_of_forecast_file)

    if watershed_dict['flood_waterlc_and_strm_cells'] or folder.FloodVELFile:
        out_file.write(f'\nFlood_WaterLC_and_STRM_Cells\t{str(watershed_dict["flood_waterlc_and_strm_cells"])}')
        out_file.write(f'\nLU_Raster_SameRes	' + folder.LAND_File)
        out_file.write(f'\nLAND_WaterValue\t{str(watershed_dict["land_watervalue"])}')

    Forecast_Flood_Map_Shapefile = Forecast_Flood_Map_Raster.replace('.tif','.shp')
    if watershed_dict['remove_old_forecast_files']:
        # delete the old FloodSpreader outputs, it can cause issues otherwise
        if os.path.exists(Forecast_Flood_Map_Raster):
            os.remove(Forecast_Flood_Map_Raster)
        if os.path.exists(Forecast_Flood_Map_Shapefile):
            os.remove(Forecast_Flood_Map_Shapefile)
        if os.path.exists(Forecast_Flood_Depth_Raster):
            os.remove(Forecast_Flood_Depth_Raster)
        if os.path.exists(Forecast_Flood_WSE_Raster):
            os.remove(Forecast_Flood_WSE_Raster)
        if os.path.exists(Forecast_Flood_VEL_Raster):
            os.remove(Forecast_Flood_VEL_Raster)

    out_file.write(f'\nOutFLD	' + Forecast_Flood_Map_Raster)
    out_file.write(f'\nOutSHP	' + Forecast_Flood_Map_Shapefile)
    out_file.write(f'\nOutDEP    ' + Forecast_Flood_Depth_Raster)
    out_file.write(f'\nOutWSE    ' + Forecast_Flood_WSE_Raster)
    out_file.write(f'\nOutVEL    ' + Forecast_Flood_VEL_Raster)
    out_file.write(f'\nLU_Manning_n	' + folder.mannings_n_text_file)
    out_file.close()
    return (Forecast_Flood_Map_Raster, Forecast_Flood_Depth_Raster)
    

def Create_BaseLine_Manning_n_File(ManningN):
    with open(ManningN, 'w') as out_file:
        out_file.write('LC_ID	Description	Manning_n')
        out_file.write(f'\n11	Water	0.030')
        out_file.write(f'\n21	Dev_Open_Space	0.013')
        out_file.write(f'\n22	Dev_Low_Intesity	0.050')
        out_file.write(f'\n23	Dev_Med_Intensity	0.075')
        out_file.write(f'\n24	Dev_High_Intensity	0.100')
        out_file.write(f'\n31	Barren_Land	0.030')
        out_file.write(f'\n41	Decid_Forest	0.120')
        out_file.write(f'\n42	Evergreen_Forest	0.120')
        out_file.write(f'\n43	Mixed_Forest	0.120')
        out_file.write(f'\n52	Shrub	0.050')
        out_file.write(f'\n71	Grass_Herb	0.030')
        out_file.write(f'\n81	Pasture_Hay	0.040')
        out_file.write(f'\n82	Cultivated_Crops	0.035')
        out_file.write(f'\n90	Woody_Wetlands	0.100')
        out_file.write(f'\n95	Emergent_Herb_Wet	0.100')

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
        layers=[layer_name] if StrmSHP.lower().endswith(".gpkg") else None  # Fix layers param
    )

    # Clean up
    source_ds = None
    LOG.info(f"Rasterization complete: {STRM_File}")

    return


# def Create_AR_StrmRaster(StrmSHP, STRM_File, outputBounds, minx, miny, maxx, maxy, dx, dy, ncols, nrows, Param):
#     print(StrmSHP)
#     source_ds = gdal.OpenEx(StrmSHP)

#     gdal.Rasterize(STRM_File, source_ds, format='GTiff', outputType=gdal.GDT_Int32, outputBounds = outputBounds, width = ncols, height = nrows, noData = -9999, attribute = Param)
#     source_ds = None

#     return

def Write_Output_Raster(s_output_filename, raster_data, ncols, nrows, dem_geotransform, dem_projection, s_file_format, s_output_type):   
    o_driver = gdal.GetDriverByName(s_file_format)  #Typically will be a GeoTIFF "GTiff"
    
    # Construct the file with the appropriate data shape
    o_output_file = o_driver.Create(s_output_filename, xsize=ncols, ysize=nrows, bands=1, eType=s_output_type)

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
    dataset = gdal.Open(InRAST_Name, gdal.GA_ReadOnly)     
    # Retrieve dimensions of cell size and cell count then close DEM dataset
    geotransform = dataset.GetGeoTransform()
    # Continue grabbing geospatial information for this use...
    band = dataset.GetRasterBand(1)
    RastArray = band.ReadAsArray()
    #global ncols, nrows, cellsize, yll, yur, xll, xur
    ncols=band.XSize
    nrows=band.YSize
    band = None
    cellsize = geotransform[1]
    yll = geotransform[3] - nrows * np.fabs(geotransform[5])
    yur = geotransform[3]
    xll = geotransform[0];
    xur = xll + (ncols)*geotransform[1]
    lat = np.fabs((yll+yur)/2.0)
    Rast_Projection = dataset.GetProjectionRef()
    dataset = None
    LOG.info('Spatial Data for Raster File:')
    LOG.info('   ncols = ' + str(ncols))
    LOG.info('   nrows = ' + str(nrows))
    LOG.info('   cellsize = ' + str(cellsize))
    LOG.info('   yll = ' + str(yll))
    LOG.info('   yur = ' + str(yur))
    LOG.info('   xll = ' + str(xll))
    LOG.info('   xur = ' + str(xur))
    return RastArray, ncols, nrows, cellsize, yll, yur, xll, xur, lat, geotransform, Rast_Projection

def Clean_STRM_Raster(STRM_File, STRM_File_Clean):
    LOG.info('\nCleaning up the Stream File.')
    (SN, ncols, nrows, _, _, _, _, _, _, dem_geotransform, dem_projection) = Read_Raster_GDAL(STRM_File)

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
    
    for filterpass in range(2):
        #First pass is just to get rid of single cells hanging out not doing anything
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        n=0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
                LOG.info(' ' + str(p_count))
            r=RR[x]
            c=CC[x]
            V = B[r,c]
            if V>0:
                #Left and Right cells are zeros
                if B[r,c+1]==0 and B[r,c-1]==0:
                    #The bottom cells are all zeros as well, but there is a cell directly above that is legit
                    if (B[r+1,c-1]+B[r+1,c]+B[r+1,c+1])==0 and B[r-1,c]>0:
                        B[r,c] = 0
                        n=n+1
                    #The top cells are all zeros as well, but there is a cell directly below that is legit
                    elif (B[r-1,c-1]+B[r-1,c]+B[r-1,c+1])==0 and B[r+1,c]>0:
                        B[r,c] = 0
                        n=n+1
                #top and bottom cells are zeros
                if B[r,c]>0 and B[r+1,c]==0 and B[r-1,c]==0:
                    #All cells on the right are zero, but there is a cell to the left that is legit
                    if (B[r+1,c+1]+B[r,c+1]+B[r-1,c+1])==0 and B[r,c-1]>0:
                        B[r,c] = 0
                        n=n+1
                    elif (B[r+1,c-1]+B[r,c-1]+B[r-1,c-1])==0 and B[r,c+1]>0:
                        B[r,c] = 0
                        n=n+1
        LOG.info('\nFirst pass removed ' + str(n) + ' cells')
        
        
        #This pass is to remove all the redundant cells
        n=0
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
                LOG.info(' ' + str(p_count) )
            r=RR[x]
            c=CC[x]
            V = B[r,c]
            if V>0:
                if B[r+1,c]==V and (B[r+1,c+1]==V or B[r+1,c-1]==V):
                    if sum(B[r+1,c-1:c+2])==0:
                        B[r+1,c] = 0
                        n=n+1
                elif B[r-1,c]==V and (B[r-1,c+1]==V or B[r-1,c-1]==V):
                    if sum(B[r-1,c-1:c+2])==0:
                        B[r-1,c] = 0
                        n=n+1
                elif B[r,c+1]==V and (B[r+1,c+1]==V or B[r-1,c+1]==V):
                    if sum(B[r-1:r+1,c+2])==0:
                        B[r,c+1] = 0
                        n=n+1
                elif B[r,c-1]==V and (B[r+1,c-1]==V or B[r-1,c-1]==V):
                    if sum(B[r-1:r+1,c-2])==0:
                            B[r,c-1] = 0
                            n=n+1
        LOG.info('\nSecond pass removed ' + str(n) + ' redundant cells')
    
    LOG.info('Writing Output File ' + STRM_File_Clean)
    Write_Output_Raster(STRM_File_Clean, B[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Int32)
    #return B[1:nrows+1,1:ncols+1], ncols, nrows, cellsize, yll, yur, xll, xur
    return

def Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(folder: FloodFolder, watervalue):
    LOG.info('Cannot find initial flood file, so creating ' + folder.FloodMapFile_Initial)
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
    LC = np.where(LC == watervalue, 1, -9999)

    # Mark streams in SN with 1, other areas with 0
    SN = (SN > 0).astype(np.uint8)

    # Combine LC and SN, prioritizing SN values
    F = np.where(SN == 1, 1, LC)

    # Mark non-stream areas as -9999 in the final flood map
    F[F <= 0] = -9999
    
    Write_Output_Raster(folder.FloodMapFile_Initial, F, ncols, nrows, sn_geotransform, sn_projection, "GTiff", gdal.GDT_Int32)

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
    # FIST Input Creation
    OutProjection = "EPSG:4269"
    # SEED file for creating a GEOJSON for FIST
    SEED_File = os.path.join(folder.FIST_Folder, folder.FileName + '_Seed.shp') 
    # ForecastFlowFile  = r"C:\Projects\2023_MultiModelFloodMapping\Yellowstone_HydroDEM\Yellowstone_flood_2022_max_streamflow_estimate.csv"
    streamflow_forecast_df = pd.read_csv(folder.ForecastFlowFile)
    
    streamflow_columns = streamflow_forecast_df.select_dtypes(include=['float']).columns.tolist()
    stream_id_field, ds_stream_id_field = get_streamids_from_source(watershed_dict['streamflow_source'])

    for streamflow_column in streamflow_columns:
        streamflow_forecast_filtered_df = streamflow_forecast_df[['rivid', streamflow_column]]
        GeoJSON_File = os.path.join(folder.FIST_Folder, f"{folder.FileName}_{watershed_dict['forensic_forecast_date']}_{streamflow_column}.geojson") 
        if not os.path.exists(GeoJSON_File):
            LOG.info('Creating FIST Input: ' + GeoJSON_File)
            with timer('geojson_forecast'):
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
    if os.path.exists(folder.DEM_File_Clean) or not watershed_dict['clean_dem']:
        return
    
    Curve_File_Initial = folder.Curve_File.replace('_CurveFile.csv','_CurveFile_Initial.csv')
    if not os.path.exists(Curve_File_Initial):
        # start time for the simulation
        with timer('arc_initial'):
            arc = Arc(folder.ARC_FileName_Initial)
            arc.run() 
    else:
        LOG.info(f"{Curve_File_Initial} exists and we aren't making it again...")

    if watershed_dict['mapper'] == "FloodSpreader" and watershed_dict['use_specified_depth_for_bathy_mask']:
        # Resolve the path to floodspreader.py
        script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
        floodspreader_path = os.path.join(script_dir, "floodspreader.py")
        # Build the subprocess call with the full path
        with timer('floodspreaderpy_initial'):
            call_mapper = f'python "{floodspreader_path}" {folder.ARC_FileName_Initial}'
            subprocess.call(call_mapper, shell=True)
    elif (watershed_dict['mapper'] in ["Curve2Flood", "FLDPLN"]) and watershed_dict['use_specified_depth_for_bathy_mask']:
        # start time for the simulation
        with timer('curve2flood_initial'):
            LOG.info(f"Executing Curve2Flood using {folder.ARC_FileName_Initial}")
            Curve2Flood_MainFunction(folder.ARC_FileName_Initial)
    
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
                                        folder.Curve_File.replace('_CurveFile.csv','_CurveFile_Initial.csv'), 
                                        folder.FloodMapFile_Initial, 
                                        Q_Fraction, 
                                        TopWidthPlausibleLimit, 
                                        search_dist_for_min_elev, 
                                        search_dist_perp_cells)

def create_bathymetry(folder: FloodFolder, watershed_dict: dict, timer: Timer):
    # Create a Bathymetry Raster Dataset
    if not os.path.exists(folder.FS_BathyFile):
        LOG.info('Cannot find bathy file, so creating ' + folder.FS_BathyFile)
        if not os.path.exists(folder.ARC_BathyFile):
            # start time for the simulation
            with timer('arc_bathy'):
                arc = Arc(folder.ARC_FileName_Bathy)
                arc.run()
        else:
            LOG.info(f"{folder.ARC_BathyFile} exists and we aren't making it again...")   

        if watershed_dict['mapper'] == "FloodSpreader":
            # Resolve the path to floodspreader.py
            script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
            floodspreader_path = os.path.join(script_dir, "floodspreader.py")
            # Build the subprocess call with the full path
            call_mapper = f'python "{floodspreader_path}" {folder.ARC_FileName_Bathy}'
            # start time for the simulation
            with timer('floodspreaderpy_bathy'):
                    subprocess.call(call_mapper, shell=True)
        elif (watershed_dict['mapper'] in ["Curve2Flood", "FLDPLN"]):
            LOG.info(f"Executing Curve2Flood using {folder.ARC_FileName_Bathy}")
            # start time for the simulation
            with timer('curve2flood_bathy'):
                Curve2Flood_MainFunction(folder.ARC_FileName_Bathy)
    else:
        LOG.info(f"{folder.FS_BathyFile} exists and we aren't making it again...")

def run_forecast_floodmapping(folder: FloodFolder, watershed_dict: dict, timer: Timer):
    if not watershed_dict.get('overwrite_forecast_floodmaps', True) and os.path.exists(folder.Forecast_Flood_Map):
        LOG.info(f"{folder.Forecast_Flood_Map} exists and we aren't overwriting it...")
        return
    
    # (May want to look here to do a reduced flow file to only flows that exceed a threshold.  Could also do in the forecast flow function)
    LOG.info('Creating Forecast Flood Event to be stored here: ' + folder.Forecast_Flood_Map)
    if watershed_dict['mapper'] == "FloodSpreader":
        # Resolve the path to floodspreader.py
        script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
        floodspreader_path = os.path.join(script_dir, "floodspreader.py")
        # Build the subprocess call with the full path
        call_mapper = f'python "{floodspreader_path}" {folder.ARC_FileName_FloodForecast}'
        # start time for the simulation
        with timer('floodspreaderpy_forecast'):
            subprocess.call(call_mapper, shell=True)
    elif (watershed_dict['mapper'] == "Curve2Flood" or watershed_dict['mapper'] == "FLDPLN"):
        LOG.info(f"Executing Curve2Flood using {folder.ARC_FileName_FloodForecast}")
        # start time for the simulation
        with timer('curve2flood_forecast'):
            Curve2Flood_MainFunction(folder.ARC_FileName_FloodForecast)
    LOG.info('Forecast Flood Raster saved here : ' + folder.Forecast_Flood_Map)
    LOG.info('Forecast Flood Shapefile saved here : ' + folder.Forecast_Flood_Map.replace('.tif','.shp'))
    # LOG.info('Forecast Flood Shapefile saved here : ' + folder.Forecast_Flood_Map.replace('.tif','.gpkg'))

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
        Forecast_Flood_Depth_Raster_Name = os.path.basename(folder.Forecast_Flood_Depth_Raster)
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


def DEM_Forecast(DEM: str, folder: FloodFolder, watershed_dict: dict, timer: Timer):
    if not (DEM.endswith(".tif") or DEM.endswith(".img")):
        return
        
    folder.setup_folder_for_dem(DEM, watershed_dict['streamflow_source'], watershed_dict['clean_dem'])
    folder.set_source_landcover_files(ESA.download_and_process_land_cover(folder))

    # This function sets-up the Input files for ARC and FloodSpreader
    # It also does some of the geospatial processing
    Process_FloodForecasting_Geospatial_Data(folder, watershed_dict, DEM)  

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
    if watershed_dict['use_specified_depth_for_bathy_mask'] is False:
        if folder.FloodMapFile_Initial is not None and not os.path.exists(folder.FloodMapFile_Initial):
            #Create an Initial Flood Map Based on Stream Raster and Land Cover Dataset
            Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(folder, 80)
        else:
            LOG.info(f"{folder.FloodMapFile_Initial} exists and we aren't making it again...")
    
    if watershed_dict['clean_dem'] and not os.path.exists(folder.FloodMapFile_Initial):
        #Create an Initial Flood Map Based on Stream Raster and Land Cover Dataset
        Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(folder, 80)

    run_dem_cleaner(folder, watershed_dict, timer, DEM)
    create_bathymetry(folder, watershed_dict, timer)

    # if the mapper is FLDPLN, then we need to remake the flood direction raster using the bathymetry output from Curve2Flood
    if watershed_dict['mapper'] == "FLDPLN":
        LOG.info("Running FLDPLN to create flood direction raster...")
        if os.path.exists(folder.flowdir_bathy):
            LOG.info("The flow direction raster we are using to run FLDPLN already exists and we are not making it again...\n")
        else:
            Hydroterrain_Processing.create_flow_direction_raster(folder.FS_BathyFile, folder.output_dir, folder.flowdir_bathy)
    
    run_forecast_floodmapping(folder, watershed_dict, timer)
    if watershed_dict['make_fist_inputs']:
        create_fist_inputs(folder, watershed_dict, timer)
    if watershed_dict['estimate_consequences']:
        run_go_consequences(folder, timer)

    return

_STREAMS_GDF_CACHE: dict[str, gpd.GeoDataFrame] = {}


def process_river_geometry(folder: FloodFolder, watershed_dict: dict, DEM: str):
    if not watershed_dict['process_stream_network']:
        return None
    
    #Datasets that can be good for a large domain
    StrmSHP: str = watershed_dict['flowline']
    if StrmSHP in _STREAMS_GDF_CACHE:
        LOG.info('Using cached stream file: ' + StrmSHP)
        StrmShp_gdf = _STREAMS_GDF_CACHE[StrmSHP]
    else:
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
    dem_proj = dem_dataset.GetProjection()  # Get the projection as a WKT string
    dem_spatial_ref = osr.SpatialReference()
    dem_spatial_ref.ImportFromWkt(dem_proj)
    # dem_crs = dem_spatial_ref.ExportToProj4()  # Export CRS to a Proj4 string (or other formats if needed)
    # Get the EPSG code
    dem_spatial_ref.AutoIdentifyEPSG()
    dem_epsg_code = dem_spatial_ref.GetAuthorityCode(None)  # This extracts the EPSG code as a string
    has_different_crs = int(str(StrmShp_gdf.crs)[5:]) != int(dem_epsg_code)

    # get bounding box of the DEM
    gt = dem_dataset.GetGeoTransform()
    minx = gt[0]
    maxx = gt[0] + (gt[1] * dem_dataset.RasterXSize)
    miny = gt[3] + (gt[5] * dem_dataset.RasterYSize)
    maxy = gt[3]

    if has_different_crs:
        minx, miny, maxx, maxy = gpd.GeoSeries(box(minx, miny, maxx, maxy), crs=dem_epsg_code).to_crs(StrmShp_gdf.crs).total_bounds

    # Filter to area of interest; avoids heavy memory use and is faster
    StrmShp_gdf = StrmShp_gdf.cx[minx:maxx, miny:maxy]

    # Check if the CRS of the shapefile matches the DEM's CRS
    if has_different_crs:
        LOG.info("DEM and Stream Network have different coordinate systems...")
        LOG.info(f"Stream CRS: {str(StrmShp_gdf.crs)[5:]}")
        LOG.info(f"DEM CRS: {dem_epsg_code}")
        # Reproject the shapefile to match the DEM's CRS
        StrmShp_gdf = StrmShp_gdf.to_crs(dem_epsg_code)

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

def print_simulation_times(watershed_dict: dict, timer: Timer):
    # here are the simulation times for each of the processes for the watershed
    LOG.info(f"Here are the simulation times for each of the processes for the watershed {watershed_dict['name']}:\n")
    LOG.info(f"ARC Initial Flood Simulation Time: {timer.get_time_string('arc_initial')}")
    LOG.info(f"Curve2Flood Initial Flood Simulation Time: {timer.get_time_string('curve2flood_initial')}")
    LOG.info(f"FloodSpreaderPy Initial Flood Simulation Time: {timer.get_time_string('floodspreaderpy_initial')}")
    LOG.info(f"DEM Cleaner Simulation Time: {timer.get_time_string('dem_cleaner')}")
    LOG.info(f"ARC Bathymetry Simulation Time: {timer.get_time_string('arc_bathy')}")
    LOG.info(f"Curve2Flood Bathymetry Simulation Time: {timer.get_time_string('curve2flood_bathy')}")
    LOG.info(f"FloodSpreaderPy Bathymetry Simulation Time: {timer.get_time_string('floodspreaderpy_bathy')}")
    LOG.info(f"Curve2Flood Forecast Simulation Time: {timer.get_time_string('curve2flood_forecast')}")
    LOG.info(f"FloodSpreaderPy Forecast Simulation Time: {timer.get_time_string('floodspreaderpy_forecast')}")
    LOG.info(f"GeoJSON Forecast Simulation Time: {timer.get_time_string('geojson_forecast')}")
    LOG.info(f"Go-Consequences Simulation Time: {timer.get_time_string('go_consequences')}")


def process_dem(watershed_dict: dict):
    LOG.info(f"Estimate consequences is set to {watershed_dict['estimate_consequences']}")

    # API key for NWM requests (required when streamflow_source is NWM)
    if watershed_dict['streamflow_source'].upper().startswith("NWM") and not watershed_dict.get('nwm_api_key'):
        raise ValueError("nwm_api_key is required when streamflow_source is NWM.")

    #Folder Management
    folder = FloodFolder(watershed_dict)

    # Validate data
    if watershed_dict.get('mapper') not in ["FloodSpreader", "Curve2Flood", "FLDPLN"]:
        raise ValueError("Invalid mapper specified. Choose 'FloodSpreader', 'Curve2Flood', or 'FLDPLN'.")
    
    if watershed_dict.get('mapper') == 'FLDPLN':
        if not all([watershed_dict.get('StrmOrder_Field'), watershed_dict.get('Downstream_Link_Field')]):
            raise ValueError("StrmOrder_Field and Downstream_Link_Field must be specified when using 'FLDPLN' mapper.")
    
    Create_BaseLine_Manning_n_File_ESA(folder.mannings_n_text_file)

    # If you are using the warning flags to download the DEM data, do it now
    if watershed_dict['use_warning_flags_to_download_dem']:
        # This outputs a list of DEMs were GEOGLOWS has forecasted flooding (2-year exceedance or above)
        DEM_List = ForecastFlows.Download_USGS_DEM_Data_Using_WarningFlag_Data(
            watershed_dict['geoglows_vpu'], folder.dem_folder, watershed_dict['forensic_forecast_date'])
    else:
        #This is the list of all the DEM files we will go through
        DEM_List = os.listdir(folder.dem_folder)
    
    # if DEM List is empty, then we need to break out of the function
    if len(DEM_List) == 0:
        LOG.info("No DEMs found in the specified folder.")
        return
    
    timer = Timer()

    #Now go through each DEM dataset
    for DEM in DEM_List:
        DEM_Forecast(DEM, folder, watershed_dict, timer)
    
    remove_landcover_tiles(folder)
    print_simulation_times(watershed_dict, timer)
    return
    
def process_json_input_serial(json_file):
    """Process input from a JSON file."""
    with open(json_file, 'r') as file:
        LOG.info(f'Opening JSON file: {json_file}')
        data = json.load(file)
        LOG.info(f"{os.linesep}{pprint.pformat(data)}")
    
    watersheds = data.get("watersheds", [])
    for watershed in watersheds:
        watershed_name = watershed.get("name")
        flowline = os.path.normpath(watershed.get("flowline"))
        dem_dir = os.path.normpath(watershed.get("dem_dir"))
        output_dir = os.path.normpath(watershed.get("output_dir"))

        # loading these early to do some preprocessing checks
        clean_dem = watershed.get("clean_dem", False)
        use_specified_depth_for_bathy_mask = watershed.get("use_specified_depth_for_bathy_mask", True)
        specify_depths_for_bathy_mask = watershed.get("specify_depths_for_bathy_mask", None)

        # Check for flood_waterlc_and_strm_cells and land_watervalue
        flood_waterlc_and_strm_cells = watershed.get("flood_waterlc_and_strm_cells", False)
        land_watervalue = watershed.get("land_watervalue", 80)
        
        # check if forensic_forecast_date and forensic_forecast_hour is provided in the watershed dictionary and if not set forensic_forecast_date=None
        # get forensic forecast date (string like "20231125" or "2023-11-25 06:00:00 UTC")
        forensic_forecast_date = watershed.get("forensic_forecast_date", None)
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
            if forensic_forecast_date_dt < datetime(2024, 7, 1):
                LOG.warning(f"Warning: Forensic forecast date {forensic_forecast_date} is earlier than July 1, 2024. Exiting...")
                sys.exit()
        else:
            forensic_forecast_date = None
            LOG.info("Forensic forecast date not provided; defaulting to None.")

        # get forensic forecast hour (needed for NWM)
        forensic_forecast_hour = watershed.get("forensic_forecast_hour", None)
        if forensic_forecast_hour is not None:
            try:
                forensic_forecast_hour = int(forensic_forecast_hour)
                if forensic_forecast_hour not in range(0, 24):
                    raise ValueError("forensic_forecast_hour must be between 0 and 23")
            except ValueError:
                raise ValueError(f"Invalid forensic_forecast_hour: {forensic_forecast_hour}")
                
        # read "streamflow_source" from the watershed dictionary, default to "GEOGLOWS" if not provided
        streamflow_source = watershed.get("streamflow_source", "GEOGLOWS")
        short_range_forecast_hours = [f"{i:02d}" for i in range(0, 24)]
        medium_range_forecast_hours = ["00", "06", "12", "18"]
        long_range_forecast_hours = ["00"]
        # if the streamflow_source is "NWM_short_range" the forensic_forecast_hour can be between 0 and 23
        if streamflow_source == "NWM_short_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in short_range_forecast_hours):
            raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be between 0 and 23 when 'streamflow_source' is 'NWM_short_range'.")
        # if the streamflow_source is "NWM_medium_range" the forensic_forecast_hour can be between one of 0, 6, 12, or 18
        if streamflow_source == "NWM_medium_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in medium_range_forecast_hours):
            raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be one of 0, 6, 12, or 18 when 'streamflow_source' is 'NWM_medium_range'.")
        # if the streamflow_source is "NWM_long_range" the forensic_forecast_hour can be only 0
        if streamflow_source == "NWM_long_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in long_range_forecast_hours):
            raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be 0 when 'streamflow_source' is 'NWM_long_range'.")

        nwm_api_key = watershed.get("nwm_api_key")
        if streamflow_source.upper().startswith("NWM") and not nwm_api_key:
            raise ValueError(f"Watershed '{watershed_name}' requires 'nwm_api_key' when 'streamflow_source' is NWM.")

        # Validation for `use_specified_depth_for_bathy_mask` and `specify_depths_for_bathy_mask`
        if use_specified_depth_for_bathy_mask is True:
            if not specify_depths_for_bathy_mask or not isinstance(specify_depths_for_bathy_mask, list) or len(specify_depths_for_bathy_mask) < 1:
                raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of two floats when 'use_specified_depth_for_bathy_mask' is True.")
            elif len(specify_depths_for_bathy_mask) < 2 and clean_dem is True:
                raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of two floats when 'clean_dem' is True.")
            elif len(specify_depths_for_bathy_mask) > 1 and clean_dem is False:
                raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of one float when 'clean_dem' is False.")
            
        overwrite_forecast_floodmaps = watershed.get("overwrite_forecast_floodmaps", True)
        remove_old_forecast_files = watershed.get("remove_old_forecast_files", False)
        make_fist_inputs = watershed.get("make_fist_inputs", True)
            
        watershed_dict = {
            "name": watershed_name,
            "flowline": flowline,
            "dem_dir": dem_dir,
            "output_dir": output_dir,
            "bathy_use_banks": watershed.get("bathy_use_banks", False),
            "flood_waterlc_and_strm_cells":flood_waterlc_and_strm_cells,
            "land_watervalue": land_watervalue,
            "clean_dem": clean_dem,
            "mapper": watershed.get("mapper", "FloodSpreader"),
            "process_stream_network": watershed.get("process_stream_network", False),
            "use_specified_depth_for_bathy_mask": use_specified_depth_for_bathy_mask,
            "age_of_forecast_days": watershed.get("age_of_forecast_days", 7),
            "find_banks_based_on_landcover": watershed.get("find_banks_based_on_landcover", True),
            "specify_depths_for_bathy_mask": specify_depths_for_bathy_mask,
            "create_reach_average_curve_file": watershed.get("create_reach_average_curve_file", False),
            "use_warning_flags_to_download_dem": watershed.get("use_warning_flags_to_download_dem", False),
            "geoglows_vpu":watershed.get("geoglows_vpu", None),
            "forensic_forecast_date": forensic_forecast_date,
            "forensic_forecast_hour": forensic_forecast_hour,
            "specified_bathyflow_field":watershed.get("specified_bathyflow_field", "p_exceed_50"),
            "specified_highflow_field":watershed.get("specified_highflow_field", "rp100_premium"),
            "StrmOrder_Field": watershed.get("StrmOrder_Field", None),
            "Downstream_Link_Field": watershed.get("Downstream_Link_Field", None),
            "StrmOrder_Lower": watershed.get("StrmOrder_Lower", None),
            "StrmOrder_Upper": watershed.get("StrmOrder_Upper", None),
            "lake_filter_json": watershed.get("lake_filter_json", None),
            "estimate_consequences": watershed.get("estimate_consequences", False),
            "streamflow_source": watershed.get("streamflow_source", "GEOGLOWS"),
            "nwm_api_key": nwm_api_key,
            "overwrite_forecast_floodmaps": overwrite_forecast_floodmaps,
            "remove_old_forecast_files": remove_old_forecast_files,
            "make_fist_inputs": make_fist_inputs
        }

        # Ensure the output directory exists
        os.makedirs(output_dir, exist_ok=True)

        LOG.info(f"Processing watershed: {watershed_name} with parameters:{os.linesep}{pprint.pformat(watershed_dict)}")

        # Call your existing processing logic here
        process_dem(watershed_dict)


def process_single_watershed(watershed):
    """Run a single watershed processing job and log to a file."""
    watershed_name = watershed.get("name", "unnamed")
    safe_name = watershed_name.replace(" ", "_").replace("/", "_")
    log_dir = os.path.join("logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{safe_name}.log")

    with open(log_path, 'w') as log_file, redirect_stdout(log_file), redirect_stderr(log_file):
        try:
            _process_watershed(watershed)
        except Exception as e:
            LOG.error(f"Exception during processing {watershed_name}: {e}")

def _process_watershed(watershed):
    """The core logic for processing a watershed. Assumes quiet context."""
    watershed_name = watershed.get("name")
    flowline = os.path.normpath(watershed.get("flowline"))
    dem_dir = os.path.normpath(watershed.get("dem_dir"))
    output_dir = os.path.normpath(watershed.get("output_dir"))

    clean_dem = watershed.get("clean_dem", False)
    use_specified_depth_for_bathy_mask = watershed.get("use_specified_depth_for_bathy_mask", True)
    specify_depths_for_bathy_mask = watershed.get("specify_depths_for_bathy_mask", None)

    flood_waterlc_and_strm_cells = watershed.get("flood_waterlc_and_strm_cells", False)
    land_watervalue = watershed.get("land_watervalue", 80) if flood_waterlc_and_strm_cells else None

    # check if forensic_forecast_date is provided in the watershed dictionary and if not set forensic_forecast_date=None
    if "forensic_forecast_date" in watershed:
        forensic_forecast_date = watershed["forensic_forecast_date"]
        if not isinstance(forensic_forecast_date, str):
            forensic_forecast_date = None
    elif "forensic_forecast_date" not in watershed:
        forensic_forecast_date = None

    # check if forensic_forecast_date is provided in the watershed dictionary and if not set forensic_forecast_date=None
    if "forensic_forecast_hour" in watershed:
        forensic_forecast_hour = watershed["forensic_forecast_hour"]
        if not isinstance(forensic_forecast_hour, str):
            forensic_forecast_hour = None
    elif "forensic_forecast_hour" not in watershed:
        forensic_forecast_hour = None
    
    # read "streamflow_source" from the watershed dictionary, default to "GEOGLOWS" if not provided
    streamflow_source = watershed.get("streamflow_source", "GEOGLOWS")
    
    # if the streamflow_source is "NWM_short_range" the forensic_forecast_hour can be between 0 and 23, the forensic_forecast_hour must be provided as a two-digit string
    short_range_forecast_hours = [f"{i:02d}" for i in range(0, 24)]
    medium_range_forecast_hours = ["00", "06", "12", "18"]
    long_range_forecast_hours = ["00"]
    if streamflow_source == "NWM_short_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in short_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be between 0 and 23 when 'streamflow_source' is 'NWM_short_range'.")
    # if the streamflow_source is "NWM_medium_range" the forensic_forecast_hour can be between one of 0, 6, 12, or 18
    if streamflow_source == "NWM_medium_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in medium_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be one of 0, 6, 12, or 18 when 'streamflow_source' is 'NWM_medium_range'.")
    # if the streamflow_source is "NWM_long_range" the forensic_forecast_hour can be only 0
    if streamflow_source == "NWM_long_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in long_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be 0 when 'streamflow_source' is 'NWM_long_range'.")

    nwm_api_key = watershed.get("nwm_api_key")
    if streamflow_source.upper().startswith("NWM") and not nwm_api_key:
        raise ValueError(f"Watershed '{watershed_name}' requires 'nwm_api_key' when 'streamflow_source' is NWM.")


    if use_specified_depth_for_bathy_mask is True:
        if not specify_depths_for_bathy_mask or not isinstance(specify_depths_for_bathy_mask, list) or len(specify_depths_for_bathy_mask) < 1:
            raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of two floats when 'use_specified_depth_for_bathy_mask' is True.")
        elif len(specify_depths_for_bathy_mask) < 2 and clean_dem is True:
            raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of two floats when 'clean_dem' is True.")
        elif len(specify_depths_for_bathy_mask) > 1 and clean_dem is False:
            raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of one float when 'clean_dem' is False.")

    watershed_dict = {
        "name": watershed_name,
        "flowline": flowline,
        "dem_dir": dem_dir,
        "output_dir": output_dir,
        "bathy_use_banks": watershed.get("bathy_use_banks", False),
        "flood_waterlc_and_strm_cells": flood_waterlc_and_strm_cells,
        "land_watervalue": land_watervalue,
        "clean_dem": clean_dem,
        "mapper": watershed.get("mapper", "FloodSpreader"),
        "process_stream_network": watershed.get("process_stream_network", False),
        "use_specified_depth_for_bathy_mask": use_specified_depth_for_bathy_mask,
        "age_of_forecast_days": watershed.get("age_of_forecast_days", 7),
        "find_banks_based_on_landcover": watershed.get("find_banks_based_on_landcover", True),
        "specify_depths_for_bathy_mask": specify_depths_for_bathy_mask,
        "create_reach_average_curve_file": watershed.get("create_reach_average_curve_file", False),
        "use_warning_flags_to_download_dem": watershed.get("use_warning_flags_to_download_dem", False),
        "geoglows_vpu": watershed.get("geoglows_vpu", None),
        "forensic_forecast_date": forensic_forecast_date,
        "forensic_forecast_hour": forensic_forecast_hour,
        "specified_bathyflow_field":watershed.get("specified_bathyflow_field", "p_exceed_50"),
        "specified_highflow_field":watershed.get("specified_highflow_field", "rp100_premium"),
        "StrmOrder_Field": watershed.get("StrmOrder_Field", None),
        "Downstream_Link_Field": watershed.get("Downstream_Link_Field", None),
        "StrmOrder_Lower": watershed.get("StrmOrder_Lower", None),
        "StrmOrder_Upper": watershed.get("StrmOrder_Upper", None),
        "lake_filter_json": watershed.get("lake_filter_json", None),
        "estimate_consequences": watershed.get("estimate_consequences", False),
        "streamflow_source": streamflow_source,
        "nwm_api_key": nwm_api_key,

    }

    os.makedirs(output_dir, exist_ok=True)

    LOG.info(f"Started processing watershed: {watershed_name}")
    LOG.info(f"Parameters: {json.dumps(watershed_dict, indent=2)}")

    # Your real processing logic
    process_dem(watershed_dict)

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
    if parallel:
        # Parallel path: one process per watershed, logging handled by process_single_watershed
        with mp.Pool(processes=num_workers) as pool:
            pool.map(process_single_watershed, watersheds)
    else:
        # Serial path: reuse existing serial routine (retains your validation flow)
        process_json_input_serial(json_file)

# def process_json_input(json_file, parallel=True, num_workers=3):
#     """Process multiple watersheds in parallel and log outputs to files."""
#     with open(json_file, 'r') as file:
#         LOG.info(f"Reading input file: {json_file}")
#         data = json.load(file)

#     watersheds = data.get("watersheds", [])
#     if not watersheds:
#         LOG.warning("No watersheds found.")
#         return
#     if parallel == True:
#         with mp.Pool(processes=num_workers or os.cpu_count()) as pool:
#             pool.map(process_single_watershed, watersheds)
#     elif parallel == False:
#         process_json_input_serial(json_file)
        
def normalize_path(path):
    return os.path.normpath(path)

def process_cli_arguments(args):
    """Process input from CLI arguments."""
    output_dir = args.output_dir
    watershed_name = args.watershed

    clean_dem = args.clean_dem
    use_specified_depth_for_bathy_mask = args.use_specified_depth_for_bathy_mask
    specify_depths_for_bathy_mask = args.specify_depths_for_bathy_mask

    # Check for flood_waterlc_and_strm_cells and land_watervalue
    flood_waterlc_and_strm_cells = args.flood_waterlc_and_strm_cells
    if flood_waterlc_and_strm_cells == True:
        land_watervalue = args.land_watervalue
    else:
        land_watervalue = 80

    # check if forensic_forecast_date is provided in the args and if not set forensic_forecast_date=None
    forensic_forecast_date = args.forensic_forecast_date
    if not isinstance(forensic_forecast_date, str):
        forensic_forecast_date = None

    # check if forensic_forecast_date is provided in the args and if not set forensic_forecast_date=None
    forensic_forecast_hour = args.forensic_forecast_hour
    if not isinstance(forensic_forecast_hour, str):
        forensic_forecast_hour = None
    
    # check if lake_filter_json is provided in the args and if not set lake_filter_json=None
    lake_filter_json = args.lake_filter_json
    if not isinstance(lake_filter_json, str):
        lake_filter_json = None

    # if the streamflow_source is "NWM_short_range" the forensic_forecast_hour can be between 0 and 23, the forensic_forecast_hour must be provided as a two-digit string
    short_range_forecast_hours = [f"{i:02d}" for i in range(0, 24)]
    medium_range_forecast_hours = ["00", "06", "12", "18"]
    long_range_forecast_hours = ["00"]
    if args.streamflow_source == "NWM_short_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in short_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be between 0 and 23 when 'streamflow_source' is 'NWM_short_range'.")
    # if the streamflow_source is "NWM_medium_range" the forensic_forecast_hour can be between one of 0, 6, 12, or 18
    if args.streamflow_source == "NWM_medium_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in medium_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be one of 0, 6, 12, or 18 when 'streamflow_source' is 'NWM_medium_range'.")
    # if the streamflow_source is "NWM_long_range" the forensic_forecast_hour can be only 0
    if args.streamflow_source == "NWM_long_range" and (forensic_forecast_hour is not None and forensic_forecast_hour not in long_range_forecast_hours):
        raise ValueError(f"Watershed '{watershed_name}' requires 'forensic_forecast_hour' to be 0 when 'streamflow_source' is 'NWM_long_range'.")

    nwm_api_key = args.nwm_api_key
    if args.streamflow_source.upper().startswith("NWM") and not nwm_api_key:
        raise ValueError(f"Watershed '{watershed_name}' requires '--nwm_api_key' when '--streamflow_source' is NWM.")



    # Validation for `use_specified_depth_for_bathy_mask` and `specify_depths_for_bathy_mask`
    if use_specified_depth_for_bathy_mask is True:
        if not specify_depths_for_bathy_mask or len(specify_depths_for_bathy_mask) < 1:
            raise ValueError("'--use_specified_depth_for_bathy_mask' must be specified as two floats when '--use_specified_depth_for_bathy_mask' is True.")
        elif len(specify_depths_for_bathy_mask) < 2 and clean_dem is True:
            raise ValueError("'--specify_depths_for_bathy_mask' must be specified as two floats when '--clean_dem' is True.")
        elif len(specify_depths_for_bathy_mask) > 1 and clean_dem is False:
            raise ValueError(f"Watershed '{watershed_name}' requires 'specify_depths_for_bathy_mask' as a list of one float when 'clean_dem' is False.")

    watershed_dict = {
        "name": watershed_name,
        "flowline": normalize_path(args.flowline),
        "dem_dir": normalize_path(args.dem_dir),
        "bathy_use_banks": args.bathy_use_banks,
        "flood_waterlc_and_strm_cells": flood_waterlc_and_strm_cells,
        "land_watervalue": land_watervalue,
        "clean_dem": clean_dem,
        "mapper": args.mapper,
        "output_dir": normalize_path(output_dir),
        "process_stream_network": args.process_stream_network,
        "use_specified_depth_for_bathy_mask": use_specified_depth_for_bathy_mask,
        "age_of_forecast_days": args.age_of_forecast_days,
        "find_banks_based_on_landcover": args.find_banks_based_on_landcover,
        "specify_depths_for_bathy_mask": specify_depths_for_bathy_mask,
        "create_reach_average_curve_file": args.create_reach_average_curve_file,
        "use_warning_flags_to_download_dem":args.use_warning_flags_to_download_dem,
        "geoglows_vpu":args.geoglows_vpu,
        "forensic_forecast_date": forensic_forecast_date,
        "forensic_forecast_hour": forensic_forecast_hour,
        "specified_bathyflow_field":args.specified_bathyflow_field,
        "specified_highflow_field":args.specified_highflow_field,
        "StrmOrder_Field": args.StrmOrder_Field,
        "Downstream_Link_Field": args.Downstream_Link_Field,
        "StrmOrder_Lower": args.StrmOrder_Lower,
        "StrmOrder_Upper": args.StrmOrder_Upper,
        "lake_filter_json": lake_filter_json,
        "estimate_consequences": args.estimate_consequences,
        "streamflow_source": args.streamflow_source,
        "nwm_api_key": nwm_api_key,
    }

    # Ensure the output directory exists
    os.makedirs(output_dir, exist_ok=True)

    LOG.info(f"Processing watershed: {args.watershed} with parameters: {watershed_dict}")
    LOG.info(f"Results will be saved in: {output_dir}")

    # Call the existing processing logic here
    process_dem(watershed_dict)

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
    cli_parser.add_argument("--mapper", type=str, default="Curve2Flood", choices=["FloodSpreader", "Curve2Flood", "FLDPLN"], help="Mapping method")
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
    cli_parser.add_argument("--Downstream_Link_Field", type=str, default=None, help="Downstream link field in the stream shapefile (optional)")
    cli_parser.add_argument("--StrmOrder_Lower", type=int, default=None, help="Lower bound for stream order (optional)")
    cli_parser.add_argument("--StrmOrder_Upper", type=int, default=None, help="Upper bound for stream order (optional)")
    cli_parser.add_argument("--use_warning_flags_to_download_dem", action="store_true", help="Use warning flags to download DEM data")
    cli_parser.add_argument("--geoglows_vpu", type=int, default=None, help="GEOGLOWS VPU ID (required if --use_warning_flags_to_download_dem is set to True)")
    cli_parser.add_argument("--lake_filter_json", type=str, default=None, help="Path to the lake filter JSON file (optional)")
    cli_parser.add_argument("--estimate_consequences", action="store_true", help="Estimate consequences using go-consequences")
    cli_parser.add_argument("--streamflow_source", type=str, default="GEOGLOWS", choices=["NWM", "GEOGLOWS"], help="Streamflow source for NenCarta (defaults to GEOGLOWS)")
    cli_parser.add_argument("--nwm_api_key", type=str, default=None, help="NWM API key (required when --streamflow_source is NWM)")

    # add subcommand for GUI
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
