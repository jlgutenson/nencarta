#conda env create -f **.yml
#conda activate ffs_esa_dc

# build-in imports
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import gc
import multiprocessing as mp
import sys
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import time
import pprint

# third-party imports
from arc import Arc
from arc.Create_GeoJSON import Run_Main_VDT_to_GEOJSON_Program_Stream_Vector
from curve2flood import Curve2Flood_MainFunction
from osgeo import gdal, osr
import json
import numpy as np
import pandas as pd
from pyproj import CRS, Transformer
import rasterio
import geopandas as gpd
from shapely.ops import transform
import numpy as np


# local imports
from . import streamflow_processing as HistFlows
from . import Download_Process_ForecastData as ForecastFlows
from . import DEM_Cleaner
from . import esa_download_processing as ESA
from . import gui_app
from . import Hydroterrain_Processing
from . import LOG


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


def Process_FloodForecasting_Geospatial_Data(ARC_Folder, ARC_FileName_Initial, 
                                             ARC_FileName_Bathy, ARC_FileName_FloodForecast, 
                                             DEM_File, DEM_File_Clean, LandCoverFile, 
                                             VDT_Test_File, STRM_File, STRM_File_Clean, 
                                             LAND_File, BathyFileFolder, FloodFolder, FLOW_Folder,
                                             Flow_Direction_Folder, ManningN, VDT_File, Curve_File, FloodMapFile, FloodDepthFile, FloodWSEFile, 
                                             FloodVELFile, FloodMapFile_Initial,
                                             DepthMapFile, ARC_BathyFile, FS_BathyFile, DEM_StrmShp, 
                                             DEM_Reanalsyis_FlowFile, bathy_use_banks, flood_waterlc_and_strm_cells, 
                                             land_watervalue, clean_dem, use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask, 
                                             find_banks_based_on_landcover, create_reach_average_curve_file,
                                             forensic_forecast_date, forensic_forecast_hour, specified_bathyflow_field, specified_highflow_field, 
                                             stream_ids_in_lake_list, streamflow_source, mapper, StrmOrder_Field, Downstream_Link_Field,
                                             StrmOrder_Lower, StrmOrder_Upper, nwm_api_key, StrmShp_gdf=None):

    
  
    
    #Get the Spatial Information from the DEM Raster
    (minx, miny, maxx, maxy, dx, dy, ncols, nrows, dem_geoTransform, dem_projection) = Get_Raster_Details(DEM_File)
    projWin_extents = [minx, maxy, maxx, miny]
    outputBounds = [minx, miny, maxx, maxy]  #https://gdal.org/api/python/osgeo.gdal.html
   
    #Create Land Dataset
    if os.path.isfile(LAND_File):
        LOG.info(LAND_File + ' Already Exists')
    else: 
        LOG.info('Creating ' + LAND_File) 
        # Let's make sure all the GIS data is using the same coordinate system as the DEM
        LandCoverFile = Check_and_Change_Coordinate_Systems(DEM_File, LandCoverFile)
        Create_AR_LandRaster(LandCoverFile, LAND_File, projWin_extents, dem_projection, ncols, nrows)

    # set the ID used for the stream network
    if streamflow_source.upper().startswith("NWM"):
        stream_id_field = 'COMID'
        ds_stream_id_field = 'TOCOMID'
    elif streamflow_source.upper() == "GEOGLOWS":
        stream_id_field = 'LINKNO'
        ds_stream_id_field = 'DSLINKNO'
    else:
        LOG.error(f"streamflow_source {streamflow_source} not recognized, please use either 'NWM' or 'GEOGLOWS'")
        sys.exit()

    # now we need to figure out if our DEM_StrmShp and DEM_Reanalysis_Flowfile exists and if not, create it
    if os.path.isfile(DEM_StrmShp) and os.path.isfile(DEM_Reanalsyis_FlowFile):
        LOG.info(DEM_StrmShp + ' Already Exists')
        LOG.info(DEM_Reanalsyis_FlowFile + ' Already Exists')
        DEM_StrmShp_gdf = gpd.read_file(DEM_StrmShp)
        rivids = DEM_StrmShp_gdf[stream_id_field].values
    elif os.path.isfile(DEM_StrmShp) and os.path.isfile(DEM_Reanalsyis_FlowFile) is False:
        (DEM_Reanalsyis_FlowFile, DEM_StrmShp, rivids, DEM_StrmShp_gdf) = HistFlows.Process_and_Write_Retrospective_Data_for_DEM_Tile(StrmShp_gdf, stream_id_field, DEM_File, DEM_Reanalsyis_FlowFile, DEM_StrmShp, stream_ids_in_lake_list, StrmOrder_Field, StrmOrder_Lower, StrmOrder_Upper, nwm_api_key)
    elif os.path.isfile(DEM_StrmShp) is False and os.path.isfile(DEM_Reanalsyis_FlowFile):
        (DEM_Reanalsyis_FlowFile, DEM_StrmShp, rivids, DEM_StrmShp_gdf) = HistFlows.Process_and_Write_Retrospective_Data_for_DEM_Tile(StrmShp_gdf, stream_id_field, DEM_File, DEM_Reanalsyis_FlowFile, DEM_StrmShp, stream_ids_in_lake_list, StrmOrder_Field, StrmOrder_Lower, StrmOrder_Upper, nwm_api_key)   
    elif StrmShp_gdf is not None and os.path.isfile(DEM_StrmShp) is False and os.path.isfile(DEM_Reanalsyis_FlowFile) is False:
        (DEM_Reanalsyis_FlowFile, DEM_StrmShp, rivids, DEM_StrmShp_gdf) = HistFlows.Process_and_Write_Retrospective_Data_for_DEM_Tile(StrmShp_gdf, stream_id_field, DEM_File, DEM_Reanalsyis_FlowFile, DEM_StrmShp, stream_ids_in_lake_list, StrmOrder_Field, StrmOrder_Lower, StrmOrder_Upper, nwm_api_key)

    # if the DEM_StrmShp is empty, return this function with None values
    if DEM_StrmShp_gdf is None or DEM_StrmShp_gdf.empty:
        LOG.info('DEM_StrmShp is empty, returning None values')
        return None, None, None, None, None, None, None, None, None

    #Create Stream Raster
    if os.path.isfile(STRM_File):
        LOG.info(STRM_File + ' Already Exists')
    else:
        LOG.info('Creating ' + STRM_File)
        Create_AR_StrmRaster(DEM_StrmShp, STRM_File, outputBounds, minx, miny, maxx, maxy, dx, dy, ncols, nrows, 'LINKNO')
    
    #Clean Stream Raster
    if os.path.isfile(STRM_File_Clean):
        LOG.info(STRM_File_Clean + ' Already Exists')
    else:
        LOG.info('Creating ' + STRM_File_Clean)
        Clean_STRM_Raster(STRM_File, STRM_File_Clean)


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
    if forensic_forecast_date is not None:
        forecastdate = forensic_forecast_date 
        LOG.info(f"Using forensic forecast date: {forecastdate}")
        if streamflow_source.upper().startswith("NWM"):
            forecasthour = forensic_forecast_hour
            LOG.info(f"Using forensic forecast hour: {forensic_forecast_hour}")
        elif streamflow_source.upper() == "GEOGLOWS":
            forecasthour = None
        try:
            demfilename = os.path.basename(DEM_File)
            if streamflow_source.upper() == "GEOGLOWS":
                ForecastFlowFile = os.path.join(FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{streamflow_source}_forecast.csv')
            elif streamflow_source.upper().startswith("NWM"):
                ForecastFlowFile = os.path.join(FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{forecasthour}_{streamflow_source}_forecast.csv')
            if not os.path.exists(ForecastFlowFile):
                # rivids = ForecastFlows.Get_RIVID_Values('Shapefile', parquet_file_from_geoglows, -9999, DEM_StrmShp)
                ForecastFlows.Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, ForecastFlowFile, streamflow_source, nwm_api_key)
        except:
            LOG.error('Could not process forensic forecast streamflow download, please check your date or try again later...')
            sys.exit()
    else:
        # cycle through today through 12 days ago to find the most recent day with a forecast
        found = False
        for fd in range(0,13):
            for fh in range(0,24):
                try:
                    demfilename = os.path.basename(DEM_File)
                    forecastdate, forecasthour = ForecastFlows.Get_Date_For_Forecast(fd, fh, streamflow_source) 
                    # we only need the forecast date for GEOGLOWS, for NWM we need the forecast hour as well               
                    if streamflow_source.upper() == "GEOGLOWS":
                        ForecastFlowFile = os.path.join(FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{streamflow_source}_forecast.csv')
                    elif streamflow_source.upper().startswith("NWM"):
                        ForecastFlowFile = os.path.join(FLOW_Folder, f'{demfilename[:-4]}_{str(forecastdate)}_{forecasthour}_{streamflow_source}_forecast.csv')
                    if not os.path.exists(ForecastFlowFile):
                        # rivids = ForecastFlows.Get_RIVID_Values('Shapefile', parquet_file_from_geoglows, -9999, DEM_StrmShp)
                        ForecastFlows.Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, ForecastFlowFile, streamflow_source, nwm_api_key)
                    found = True
                    break
                except Exception as e:
                    LOG.error(f'Could not process forecast, moving back another day.. ({e})')
            if found:
                break  # break outer
    LOG.info('Forecast data save here: ' + ForecastFlowFile)
    
    
    #Get the unique values for all the stream ids
    (S, ncols, nrows, cellsize, yll, yur, xll, xur, lat, dem_geotransform, dem_projection) = Read_Raster_GDAL(STRM_File_Clean)
    # delete the variables from Read_Raster_GDAL we are not using
    del cellsize, yll, yur, xll, xur, lat, dem_geotransform
    gc.collect()
    (RR,CC) = S.nonzero()
    num_strm_cells = len(RR)
    COMID_Unique = np.unique(S)
    # COMID_Unique = np.delete(COMID_Unique, 0)  #We don't need the first entry of zero
    COMID_Unique = COMID_Unique[np.where(COMID_Unique > 0)]
    COMID_Unique = np.sort(COMID_Unique).astype(int)
    num_comids = len(COMID_Unique)

    # if the mapper is "FLDPLN", we need to create a flow direction raster using the bathymetry based DEM.
    if mapper == "FLDPLN":
        projected_dem = os.path.join(Flow_Direction_Folder, os.path.basename(DEM_File).replace(".tif", "_projected.tif"))
        filled_dem = os.path.join(Flow_Direction_Folder, os.path.basename(DEM_File).replace(".tif","_filled.tif"))
        filled_dem_orig = os.path.join(Flow_Direction_Folder, os.path.basename(DEM_File).replace(".tif","_filled_orig_crs.tif"))
        flowdir = os.path.join(Flow_Direction_Folder, os.path.basename(DEM_File).replace(".tif","_flowdir.tif"))
        flowdir_orig = os.path.join(Flow_Direction_Folder, os.path.basename(DEM_File).replace(".tif","_flowdir_orig_crs.tif"))
        if os.path.exists(flowdir_orig):
            LOG.info("The flow direction raster already exists and will not be recreated...")
            pass
        else:
            Hydroterrain_Processing.create_flow_direction_raster(DEM_File, Flow_Direction_Folder, flowdir_orig)
        # name the flowdir we will use for forecasts and that will be remade after the bathymetry is burned into the DEM
        flowdir_bathy = os.path.join(Flow_Direction_Folder, os.path.basename(FS_BathyFile).replace('.tif','_FlowDir.tif'))
    else:
        flowdir_orig = None
        flowdir_bathy = None
    
    #Create a Starting AutoRoute Input File
    LOG.info('Creating ARC Input File: ' + ARC_FileName_Initial)
    #Create the Initial Flow
    LOG.info(f"Using the field '{specified_bathyflow_field}' for bathymetry estimation and '{specified_highflow_field}' for flood mapping...\n")

    COMID_Q_File = os.path.join(os.path.dirname(DEM_Reanalsyis_FlowFile), f"{os.path.basename(DEM_File[:-4])}_2yr_flow_initial.txt")
    Create_FlowFile(DEM_Reanalsyis_FlowFile, COMID_Q_File, 'COMID', 'rp2')

    # Create the Initial input file which is only used if cleaning the DEM
    if clean_dem is True:
        Create_ARC_Model_Input_File_Initial(ARC_FileName_Initial, mapper, DEM_File, COMID_Q_File, 'COMID', specified_bathyflow_field, 
                                            specified_highflow_field, STRM_File_Clean, LAND_File, DEM_Reanalsyis_FlowFile, 
                                            VDT_File, Curve_File, ManningN, FloodMapFile, VDT_Test_File, DEM_StrmShp, 
                                            bathy_use_banks, flood_waterlc_and_strm_cells, land_watervalue, 
                                            use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask, 
                                            find_banks_based_on_landcover, create_reach_average_curve_file,flowdir_orig,
                                            StrmOrder_Field, Downstream_Link_Field)

    #Create the Bathy Input File
    LOG.info('Creating ARC Input File: ' + ARC_FileName_Bathy)
    if clean_dem is True:
        Create_ARC_Model_Input_File_Bathy(ARC_FileName_Bathy, mapper, DEM_File_Clean, COMID_Q_File, 'COMID', 
                                          specified_bathyflow_field, specified_highflow_field, STRM_File_Clean, 
                                          LAND_File, DEM_Reanalsyis_FlowFile, VDT_File, Curve_File, ManningN, 
                                          FloodMapFile, FloodMapFile_Initial, ARC_BathyFile, FS_BathyFile, 
                                          VDT_Test_File, DEM_StrmShp, bathy_use_banks, flood_waterlc_and_strm_cells, 
                                          land_watervalue, use_specified_depth_for_bathy_mask, 
                                          specify_depths_for_bathy_mask, find_banks_based_on_landcover, create_reach_average_curve_file,
                                          flowdir_orig, StrmOrder_Field, Downstream_Link_Field)
    elif clean_dem is False:
        Create_ARC_Model_Input_File_Bathy(ARC_FileName_Bathy, mapper, DEM_File, COMID_Q_File, 'COMID', 
                                          specified_bathyflow_field, specified_highflow_field, STRM_File_Clean, 
                                          LAND_File, DEM_Reanalsyis_FlowFile, VDT_File, Curve_File, ManningN, 
                                          FloodMapFile, FloodMapFile_Initial, ARC_BathyFile, FS_BathyFile, 
                                          VDT_Test_File, DEM_StrmShp, bathy_use_banks, flood_waterlc_and_strm_cells, 
                                          land_watervalue, use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask,
                                          find_banks_based_on_landcover, create_reach_average_curve_file, 
                                          flowdir_orig, StrmOrder_Field, Downstream_Link_Field)

    #Create the Forecast Input File
    LOG.info('Creating ARC Input File: ' + ARC_FileName_FloodForecast)
    Forecast_Flood_Map, Forecast_Flood_Depth_Raster = Create_ARC_Model_Input_File_FloodForecast(streamflow_source, mapper, ARC_FileName_FloodForecast, ForecastFlowFile, 
                                                                                                STRM_File_Clean, VDT_File, Curve_File, ManningN, FloodMapFile, 
                                                                                                FloodDepthFile, FloodWSEFile, FloodVELFile, FS_BathyFile, 
                                                                                                forecastdate, forecasthour, DEM_StrmShp, flood_waterlc_and_strm_cells, 
                                                                                                land_watervalue, LAND_File, flowdir_bathy, StrmOrder_Field, Downstream_Link_Field)
    
    return (ARC_FileName_Initial, ARC_FileName_Bathy, ARC_FileName_FloodForecast, Forecast_Flood_Map, 
            DEM_Reanalsyis_FlowFile, ForecastFlowFile, DEM_StrmShp, forecastdate, Forecast_Flood_Depth_Raster, 
            stream_id_field, ds_stream_id_field, flowdir_bathy)

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

def Create_Folder(F):
    if not os.path.exists(F): 
        os.makedirs(F)
    return

def Create_ARC_Model_Input_File_Initial(ARC_Input_File_Initial, 
                                        mapper,
                                        DEM_File_Clean, 
                                        COMID_Q_File, 
                                        COMID_Param, 
                                        Q_BF_Param, 
                                        Q_Param, 
                                        STRM_File_Clean, 
                                        LAND_File, 
                                        FLOW_File_Use, 
                                        VDT_File, 
                                        Curve_File, 
                                        ManningN, 
                                        FloodMapFile, 
                                        VDT_Test_File, 
                                        DEM_StrmShp, 
                                        bathy_use_banks, 
                                        flood_waterlc_and_strm_cells, 
                                        land_watervalue, 
                                        use_specified_depth_for_bathy_mask, 
                                        specify_depths_for_bathy_mask, 
                                        find_banks_based_on_landcover, 
                                        create_reach_average_curve_file,
                                        flowdir_orig,
                                        StrmOrder_Field,
                                        Downstream_Link_Field):
    out_file = open(ARC_Input_File_Initial,'w')
    out_file.write('#ARC_Inputs')
    out_file.write('\n' + 'DEM_File	' + DEM_File_Clean)
    out_file.write('\n' + 'Stream_File	' + STRM_File_Clean)
    out_file.write('\n' + 'LU_Raster_SameRes	' + LAND_File)
    out_file.write('\n' + 'LU_Manning_n	' + ManningN)
    out_file.write('\n' + 'Flow_File	' + FLOW_File_Use)
    out_file.write('\n' + 'Flow_File_ID	' + COMID_Param)
    out_file.write('\n' + 'Flow_File_BF	' + Q_BF_Param)
    out_file.write('\n' + 'Flow_File_QMax	' + Q_Param)
    out_file.write('\n' + 'Spatial_Units	deg')
    out_file.write('\n' + 'X_Section_Dist	5000.0')
    out_file.write('\n' + 'Degree_Manip	6.1')
    out_file.write('\n' + 'Degree_Interval	1.5')
    out_file.write('\n' + 'Low_Spot_Range	2')
    out_file.write('\n' + 'Str_Limit_Val	1')
    out_file.write('\n' + 'Gen_Dir_Dist	10')
    out_file.write('\n' + 'Gen_Slope_Dist	10')
    out_file.write('\n' + 'Stream_Slope_Method' + '\t' + 'local_average_corrected')
    
    out_file.write('\n\n#VDT_Output_File_and_CurveFile')
    out_file.write('\n' + 'VDT_Database_NumIterations	30')
    out_file.write('\n' + 'Print_VDT_Database	' + VDT_File.replace('.txt', '_Initial.txt'))
    out_file.write('\n' + 'Print_Curve_File	' + Curve_File.replace('.csv', '_Initial.csv'))
    out_file.write('\n' + 'Reach_Average_Curve_File' + '\t' + f'{create_reach_average_curve_file}')
    
    out_file.write('\n\n#Mapper Input Data')
    out_file.write('\n' + 'StrmShp_File	' + DEM_StrmShp)
    out_file.write('\n' + 'Comid_Flow_File	' + COMID_Q_File)
    out_file.write('\n' + 'FS_ADJUST_FLOW_BY_FRACTION' + '\t' +	'1.0')
    out_file.write('\n' + 'Bathy_Use_Banks' + '\t' + str(bathy_use_banks))
    if flood_waterlc_and_strm_cells is True:
        out_file.write('\n' + 'Flood_WaterLC_and_STRM_Cells' + '\t' + str(flood_waterlc_and_strm_cells))
        out_file.write('\n' + 'LAND_WaterValue' + '\t' + str(land_watervalue))
    if find_banks_based_on_landcover is True:
        out_file.write('\n' + 'FindBanksBasedOnLandCover' + '\t' + str(find_banks_based_on_landcover))
    # out_file.write('\n' + 'FloodLocalOnly')
    if use_specified_depth_for_bathy_mask is True:
        if mapper == "FLDPLN":
            out_file.write('\n' + 'Use_FLDPLN_Model' + '\t' + "True")
            out_file.write('\n' + 'Flow_Direction_File' + '\t' + flowdir_orig)
            out_file.write('\n' + 'StrmOrder_Field' + '\t' + StrmOrder_Field)
            out_file.write('\n' + 'Downstream_Link_Field' + '\t' + Downstream_Link_Field)
            out_file.write('\n' + 'FLDPLN_fldmn' + '\t' + '0.01')
            out_file.write('\n' + 'FLDPLN_fldmx' + '\t' + '50')
            out_file.write('\n' + 'FLDPLN_dh' + '\t' + '0.5')
            out_file.write('\n' + 'FLDPLN_mxht0' + '\t' + '0.0')
            out_file.write('\n' + 'FLDPLN_ssflg' + '\t' + '1')
            out_file.write('\n' + 'Flow_Direction_File' + '\t' + flowdir_orig)
        out_file.write('\n' + 'OutFLD	' + FloodMapFile.replace('.tif', '_Initial.tif'))
        out_file.write('\n' + 'OutSHP	' + FloodMapFile.replace('.tif', '_Initial.shp'))
        # out_file.write('\n' + 'OutSHP	' + FloodMapFile.replace('.tif', '_Initial.gpkg'))
        out_file.write('\n' + f'FloodSpreader_SpecifyDepth	{specify_depths_for_bathy_mask[0]}')
    out_file.close()
    


def Create_ARC_Model_Input_File_Bathy(ARC_FileName_Bathy, mapper, DEM_File_Clean, COMID_Q_File, COMID_Param, 
                                      Q_BF_Param, Q_Param, STRM_File_Clean, LAND_File, FLOW_File_Use, VDT_File, 
                                      Curve_File, ManningN, FloodMapFile, FloodMapFile_Initial, ARC_BathyFile, 
                                      FS_BathyFile, VDT_Test_File, DEM_StrmShp, bathy_use_banks, flood_waterlc_and_strm_cells, 
                                      land_watervalue, use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask, 
                                      find_banks_based_on_landcover, create_reach_average_curve_file,
                                      flowdir_orig, StrmOrder_Field, Downstream_Link_Field):
    out_file = open(ARC_FileName_Bathy,'w')
    out_file.write('#ARC_Inputs')
    out_file.write('\n' + 'DEM_File	' + DEM_File_Clean)
    out_file.write('\n' + 'Stream_File	' + STRM_File_Clean)
    out_file.write('\n' + 'LU_Raster_SameRes	' + LAND_File)
    out_file.write('\n' + 'LU_Manning_n	' + ManningN)
    out_file.write('\n' + 'Flow_File	' + FLOW_File_Use)
    out_file.write('\n' + 'Flow_File_ID	' + COMID_Param)
    out_file.write('\n' + 'Flow_File_BF	' + Q_BF_Param)
    out_file.write('\n' + 'Flow_File_QMax	' + Q_Param)
    out_file.write('\n' + 'Spatial_Units	deg')
    out_file.write('\n' + 'X_Section_Dist	5000.0')
    out_file.write('\n' + 'Degree_Manip	6.1')
    out_file.write('\n' + 'Degree_Interval	1.5')
    out_file.write('\n' + 'Low_Spot_Range	2')
    out_file.write('\n' + 'Str_Limit_Val	1')
    out_file.write('\n' + 'Gen_Dir_Dist	10')
    out_file.write('\n' + 'Gen_Slope_Dist	10')
    out_file.write('\n' + 'Stream_Slope_Method' + '\t' + 'local_average_corrected')
    
    out_file.write('\n\n#VDT_Output_File_and_CurveFile')
    out_file.write('\n' + 'VDT_Database_NumIterations	30')
    out_file.write('\n' + 'Print_VDT_Database' + '\t' + VDT_File.replace('.txt', '_Bathy.txt'))
    out_file.write('\n' + 'Print_Curve_File' + '\t' + Curve_File.replace('.csv', '_Bathy.csv'))
    out_file.write('\n' + 'Print_VDT_Test_File' + '\t' + VDT_Test_File.replace('.txt', '_Bathy.txt'))
    AP_File_Name = VDT_File.replace('VDT_', 'AP_')
    out_file.write('\n' + 'Print_AP_Database' + '\t' + AP_File_Name.replace('.txt', '_Bathy.txt'))
    out_file.write('\n' + 'Reach_Average_Curve_File' + '\t' + f'{create_reach_average_curve_file}')
    
    out_file.write('\n\n#Mapper Input Data')
    out_file.write('\n' + 'Comid_Flow_File	' + COMID_Q_File)
    out_file.write('\n' + 'StrmShp_File	' + DEM_StrmShp)
    out_file.write('\n' + 'FS_ADJUST_FLOW_BY_FRACTION	1.0')
    # out_file.write('\n' + 'FloodLocalOnly')
    out_file.write('\n' + 'OutFLD	' + FloodMapFile.replace('.tif', '_Bathy.tif'))
    out_file.write('\n' + 'OutSHP	' + FloodMapFile.replace('.tif', '_Bathy.shp'))
    # out_file.write('\n' + 'OutSHP	' + FloodMapFile.replace('.tif', '_Bathy.gpkg'))
    out_file.write('\n' + 'TopWidthDistanceFactor' + '\t' +	'1.5')
    out_file.write('\n' + 'TW_MultFact' + '\t' +  '1.5')
    out_file.write('\n' + 'TopWidthPlausibleLimit' + '\t' + '2000')
    if use_specified_depth_for_bathy_mask is True:
        if mapper == "FLDPLN":
            out_file.write('\n' + 'Use_FLDPLN_Model' + '\t' + "True")
            out_file.write('\n' + 'Flow_Direction_File' + '\t' + flowdir_orig)
            out_file.write('\n' + 'StrmOrder_Field' + '\t' + StrmOrder_Field)
            out_file.write('\n' + 'Downstream_Link_Field' + '\t' + Downstream_Link_Field)
            out_file.write('\n' + 'FLDPLN_fldmn' + '\t' + '0.01')
            out_file.write('\n' + 'FLDPLN_fldmx' + '\t' + '50')
            out_file.write('\n' + 'FLDPLN_dh' + '\t' + '0.5')
            out_file.write('\n' + 'FLDPLN_mxht0' + '\t' + '0.0')
            out_file.write('\n' + 'FLDPLN_ssflg' + '\t' + '1')
            out_file.write('\n' + 'Flow_Direction_File' + '\t' + flowdir_orig)
        if len(specify_depths_for_bathy_mask) == 1:
            specified_depth = specify_depths_for_bathy_mask[0]
        if len(specify_depths_for_bathy_mask) == 2:
            specified_depth = specify_depths_for_bathy_mask[1]
        out_file.write('\n' + f'FloodSpreader_SpecifyDepth\t{specified_depth}')
    elif use_specified_depth_for_bathy_mask is False:
        out_file.write('\n' + 'BathyWaterMask' + '\t' + FloodMapFile_Initial)
    out_file.write('\n\n#Bathymetry_Information')
    out_file.write('\n' + 'Bathy_Trap_H' + '\t' + '0.20')
    out_file.write('\n' + 'Bathy_Use_Banks' + '\t' + str(bathy_use_banks))
    if flood_waterlc_and_strm_cells is True:
        out_file.write('\n' + 'Flood_WaterLC_and_STRM_Cells' + '\t' + str(flood_waterlc_and_strm_cells))
        out_file.write('\n' + 'LAND_WaterValue' + '\t' + str(land_watervalue))
    if find_banks_based_on_landcover is True:
        out_file.write('\n' + 'FindBanksBasedOnLandCover' + '\t' + str(find_banks_based_on_landcover))
    out_file.write('\n' + 'AROutBATHY	' + ARC_BathyFile)
    out_file.write('\n' + 'BATHY_Out_File	' + ARC_BathyFile)
    out_file.write('\n' + 'FSOutBATHY	' + FS_BathyFile)

    out_file.close()

def Create_ARC_Model_Input_File_FloodForecast(streamflow_source, mapper, ARC_FileName_FloodForecast, ForecastFlowFile, 
                                                STRM_File_Clean, VDT_File, Curve_File, ManningN, FloodMapFile, FloodDepthFile, 
                                                FloodWSEFile, FloodVELFile, FS_BathyFile, forecastdate, forecasthour, 
                                                DEM_StrmShp, flood_waterlc_and_strm_cells, land_watervalue, LAND_File,
                                                flowdir_bathy, StrmOrder_Field, Downstream_Link_Field):
    
    out_file = open(ARC_FileName_FloodForecast, 'w')
    out_file.write('#ARC_Inputs')
    out_file.write('\n' + 'DEM_File	' + FS_BathyFile)
    out_file.write('\n' + 'Stream_File	' + STRM_File_Clean)
    
    out_file.write('\n\n#VDT_Output_File_and_CurveFile')
    out_file.write('\n' + 'Print_VDT_Database	' + VDT_File.replace('.txt', '_Bathy.txt'))
    out_file.write('\n' + 'Print_Curve_File	' + Curve_File.replace('.csv', '_Bathy.csv'))
    
    out_file.write('\n\n#Mapper Input Data')
    out_file.write('\n' + 'StrmShp_File	' + DEM_StrmShp)
    out_file.write('\n' + 'Comid_Flow_File	' + ForecastFlowFile)
    if mapper == "FLDPLN":
            out_file.write('\n' + 'Use_FLDPLN_Model' + '\t' + "True")
            out_file.write('\n' + 'Flow_Direction_File' + '\t' + flowdir_bathy)
            out_file.write('\n' + 'StrmOrder_Field' + '\t' + StrmOrder_Field)
            out_file.write('\n' + 'Downstream_Link_Field' + '\t' + Downstream_Link_Field)
            out_file.write('\n' + 'FLDPLN_fldmn' + '\t' + '0.01')
            out_file.write('\n' + 'FLDPLN_fldmx' + '\t' + '50')
            out_file.write('\n' + 'FLDPLN_dh' + '\t' + '0.5')
            out_file.write('\n' + 'FLDPLN_mxht0' + '\t' + '0.0')
            out_file.write('\n' + 'FLDPLN_ssflg' + '\t' + '1')
    # out_file.write('\n' + 'Comid_Flow_File	' + r"C:\Projects\2023_MultiModelFloodMapping\Yellowstone_HydroDEM\Yellowstone_flood_2022_max_streamflow_estimate.csv")
    out_file.write('\n' + 'FS_ADJUST_FLOW_BY_FRACTION' + '\t' + '1.0')
    out_file.write('\n' + 'TW_MultFact' + '\t' +  '1.5')
    out_file.write('\n' + 'TopWidthPlausibleLimit' + '\t' + '6000')
    #out_file.write('\n' + 'FloodLocalOnly')
    if streamflow_source.upper().startswith("NWM"):
        # create the end of the file name that describes the forecast
        ending_of_forecast_file = '_Forecast_' + str(forecastdate) + '_' + str(forecasthour) + '.tif' 
        # rename the forecast of the extent raster based upon the type of NWM forecast we are using
        Forecast_Flood_Map_Raster = FloodMapFile.replace('.tif', ending_of_forecast_file)
        Forecast_Flood_Map_Raster = FloodMapFile.replace('NWM', streamflow_source)
    else:
        # create the end of the file name that describes the forecast
        ending_of_forecast_file = '_Forecast_' + str(forecastdate) + '.tif' 
        Forecast_Flood_Map_Raster = FloodMapFile.replace('.tif', ending_of_forecast_file)

    Forecast_Flood_Depth_Raster = FloodDepthFile.replace('.tif', ending_of_forecast_file)
    Forecast_Flood_WSE_Raster = FloodWSEFile.replace('.tif', ending_of_forecast_file)
    Forecast_Flood_VEL_Raster = FloodVELFile.replace('.tif', ending_of_forecast_file)
    Forecast_Flood_Map_Raster = FloodMapFile.replace('.tif', ending_of_forecast_file)
    # add the name of the file in the DEM_StrmShp file path to the Forecast_Flood_Map_Raster variable
    DEM_StrmShp_filname = os.path.basename(DEM_StrmShp)

    if flood_waterlc_and_strm_cells or FloodVELFile:
        out_file.write('\n' + 'Flood_WaterLC_and_STRM_Cells' + '\t' + str(flood_waterlc_and_strm_cells))
        out_file.write('\n' + 'LU_Raster_SameRes	' + LAND_File)
        out_file.write('\n' + 'LAND_WaterValue' + '\t' + str(land_watervalue))

    # delete the old FloodSpreader outputs, it can cause issues otherwise
    if os.path.exists(Forecast_Flood_Map_Raster):
        os.remove(Forecast_Flood_Map_Raster)
    Forecast_Flood_Map_Shapefile = Forecast_Flood_Map_Raster.replace('.tif','.shp')
    # Forecast_Flood_Map_Shapefile = Forecast_Flood_Map_Raster.replace('.tif','.gpkg')
    if os.path.exists(Forecast_Flood_Map_Shapefile):
        os.remove(Forecast_Flood_Map_Shapefile)
    if os.path.exists(Forecast_Flood_Depth_Raster):
        os.remove(Forecast_Flood_Depth_Raster)
    if os.path.exists(Forecast_Flood_WSE_Raster):
        os.remove(Forecast_Flood_WSE_Raster)
    if os.path.exists(Forecast_Flood_VEL_Raster):
        os.remove(Forecast_Flood_VEL_Raster)
    out_file.write('\n' + 'OutFLD	' + Forecast_Flood_Map_Raster)
    out_file.write('\n' + 'OutSHP	' + Forecast_Flood_Map_Shapefile)
    out_file.write('\n' + 'OutDEP    ' + Forecast_Flood_Depth_Raster)
    out_file.write('\n' + 'OutWSE    ' + Forecast_Flood_WSE_Raster)
    out_file.write('\n' + 'OutVEL    ' + Forecast_Flood_VEL_Raster)
    out_file.write('\n' + 'LU_Manning_n	' + ManningN)
    out_file.close()
    return (Forecast_Flood_Map_Raster, Forecast_Flood_Depth_Raster)
    

def Create_BaseLine_Manning_n_File(ManningN):
    out_file = open(ManningN,'w')
    out_file.write('LC_ID	Description	Manning_n')
    out_file.write('\n' + '11	Water	0.030')
    out_file.write('\n' + '21	Dev_Open_Space	0.013')
    out_file.write('\n' + '22	Dev_Low_Intesity	0.050')
    out_file.write('\n' + '23	Dev_Med_Intensity	0.075')
    out_file.write('\n' + '24	Dev_High_Intensity	0.100')
    out_file.write('\n' + '31	Barren_Land	0.030')
    out_file.write('\n' + '41	Decid_Forest	0.120')
    out_file.write('\n' + '42	Evergreen_Forest	0.120')
    out_file.write('\n' + '43	Mixed_Forest	0.120')
    out_file.write('\n' + '52	Shrub	0.050')
    out_file.write('\n' + '71	Grass_Herb	0.030')
    out_file.write('\n' + '81	Pasture_Hay	0.040')
    out_file.write('\n' + '82	Cultivated_Crops	0.035')
    out_file.write('\n' + '90	Woody_Wetlands	0.100')
    out_file.write('\n' + '95	Emergent_Herb_Wet	0.100')
    out_file.close()

def Create_BaseLine_Manning_n_File_ESA(ManningN):
    out_file = open(ManningN,'w')
    out_file.write('LC_ID	Description	Manning_n')
    out_file.write('\n' + '10	Tree Cover	0.120')
    out_file.write('\n' + '20	Shrubland	0.050')
    out_file.write('\n' + '30	Grassland	0.030')
    out_file.write('\n' + '40	Cropland	0.035')
    out_file.write('\n' + '50	Builtup	0.075')
    out_file.write('\n' + '60	Bare	0.030')
    out_file.write('\n' + '70	SnowIce	0.030')
    out_file.write('\n' + '80	Water	0.030')
    out_file.write('\n' + '90	Emergent_Herb_Wet	0.100')
    out_file.write('\n' + '95	Mangroves	0.100')
    out_file.write('\n' + '100	MossLichen	0.100')
    out_file.close()


def Create_AR_LandRaster(LandCoverFile, LAND_File, projWin_extents, out_projection, ncols, nrows):
    ds = gdal.Open(LandCoverFile)
    ds = gdal.Translate(LAND_File, ds, projWin = projWin_extents, width=ncols, height = nrows)
    ds = None
    return

from osgeo import gdal

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
    try:
        dataset = gdal.Open(InRAST_Name, gdal.GA_ReadOnly)     
    except RuntimeError:
        sys.exit(" ERROR: Field Raster File cannot be read!")
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
    (SN, ncols, nrows, cellsize, yll, yur, xll, xur, lat, dem_geotransform, dem_projection) = Read_Raster_GDAL(STRM_File)

    # delete the variables from Read_Raster_GDAL we are not using
    del yll, yur, xll, xur, lat, cellsize
    gc.collect()
    
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

def Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(FloodMapFile_Initial, STRM_File_Clean, LandCoverFile, watervalue):
    (LC, ncols, nrows, cellsize, yll, yur, xll, xur, lat, lc_geotransform, lc_projection) = Read_Raster_GDAL(LandCoverFile)
    (SN, ncols, nrows, cellsize, yll, yur, xll, xur, lat, sn_geotransform, sn_projection) = Read_Raster_GDAL(STRM_File_Clean)

    # delete the variables we are not using
    del yll, yur, xll, xur, lat, cellsize, lc_geotransform, lc_projection
    gc.collect()
    
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
    
    Write_Output_Raster(FloodMapFile_Initial, F, ncols, nrows, sn_geotransform, sn_projection, "GTiff", gdal.GDT_Int32)

    # delete all the variables we are not using
    del LC, SN, F
    del sn_geotransform, sn_projection
    gc.collect()

    return

def Check_and_Change_Coordinate_Systems(DEM_File, LandCoverFile):

    # Load the projection of the DEM file
    with rasterio.open(DEM_File) as src:
        dem_projection = src.crs
    src.close()

    # re-project the LAND file raster, if necessary
    with rasterio.open(LandCoverFile) as src:
        current_crs = src.crs
    src.close()
        
    if current_crs != dem_projection:
        input_raster = gdal.Open(LandCoverFile)
        LandCoverFile_Update = f"{LandCoverFile[:-4]}_new.tif"
        output_raster = LandCoverFile_Update
        warp = gdal.Warp(output_raster,input_raster,dstSRS=dem_projection)
        warp = None # Closes the files
        input_raster = None

    # delete the old LAND raster, if it was replaced and change the name
    if current_crs != dem_projection:
        os.remove(LandCoverFile)
        LandCoverFile = LandCoverFile_Update

    return (LandCoverFile)

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

def DEM_Forecast(DEM_Folder, DEM, Output_Dir, watershed, ESA_LC_Folder, STRM_Folder, LAND_Folder, 
                 FLOW_Folder, VDT_Folder, DEM_Updated_Folder, FloodFolder, ARC_Folder, 
                 BathyFileFolder, FIST_Folder, Flow_Direction_Folder, ManningN, bathy_use_banks, 
                 flood_waterlc_and_strm_cells, land_watervalue, mapper, clean_dem, 
                 use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask, 
                 age_of_forecast_days, find_banks_based_on_landcover, 
                 create_reach_average_curve_file, 
                 arc_initial_simulation_time, curve2flood_initial_simulation_time, floodpsreaderpy_initial_simulation_time,
                 dem_cleaner_simulation_time,
                 arc_bathy_simulation_time, curve2flood_bathy_simulation_time, floodpsreaderpy_bathy_simulation_time, 
                 curve2flood_forecast_simulation_time, floodpsreaderpy_forecast_simulation_time, geojson_forecast_simulation_time,
                 estimate_consequences, go_consequences_simulation_time,
                 forensic_forecast_date, forensic_forecast_hour, specified_bathyflow_field, specified_highflow_field, stream_ids_in_lake_list,
                 Consequences_Folder, streamflow_source, StrmOrder_Field, Downstream_Link_Field,
                 StrmOrder_Lower, StrmOrder_Upper, nwm_api_key, StrmShp_gdf=None):

    if DEM.endswith(".tif") or DEM.endswith(".img"):
        DEM_Name = DEM
        FileName = DEM_Name.replace('.tif','')
        FileName = FileName.replace('.img','')
        DEM_File = os.path.join(DEM_Folder, DEM_Name)

        # currently the land file will be the same regardless of the streamflow source
        LAND_File = os.path.join(LAND_Folder, FileName + '_LAND_Raster.tif')
        
        #Input Datasets that will 

        
        #Datasets to be Created
        DEM_StrmShp = os.path.join(STRM_Folder, f"{streamflow_source}_{FileName}_StrmShp.gpkg")
        DEM_Reanalsyis_FlowFile = os.path.join(FLOW_Folder,f"{streamflow_source}_{FileName}_Reanalysis.csv")

        # isolating the NWM or GEOGLOWS text in the streamflow_source variable
        match = re.search(r"(NWM|GEOGLOWS)", streamflow_source)
        # these will only vary based upon if they are NWM or GEOGLOWS
        ARC_FileName_Initial = os.path.join(ARC_Folder, match.group(0) + '_ARC_Input_' + FileName + '_InitialFlood.txt')
        ARC_FileName_Bathy = os.path.join(ARC_Folder, match.group(0) + '_ARC_Input_' + FileName + '_Bathy.txt')
        DEM_File_Clean = os.path.join(DEM_Updated_Folder, match.group(0) + '_' + FileName + '_Clean.tif')
        VDT_Test_File = os.path.join(VDT_Folder, match.group(0) + '_' + FileName + '_VDT_FS.csv')
        STRM_File = os.path.join(STRM_Folder, match.group(0) + '_' + FileName + '_STRM_Raster.tif')
        STRM_File_Clean = STRM_File.replace('.tif','_Clean.tif')
        VDT_File = os.path.join(VDT_Folder, match.group(0) + '_' + FileName + '_VDT_Database.txt')
        Curve_File = os.path.join(VDT_Folder, match.group(0) + '_' + FileName + '_CurveFile.csv')
        FloodMapFile_Initial = os.path.join(FloodFolder, match.group(0) + '_' + FileName + '_ARC_Flood_Initial.tif')
        DepthMapFile = os.path.join(FloodFolder, match.group(0) + '_' + FileName + '_ARC_Depth.tif')
        ARC_BathyFile = os.path.join(BathyFileFolder, match.group(0) + '_' + FileName + '_ARC_Bathy.tif')
        FS_BathyFile = os.path.join(BathyFileFolder, match.group(0) +'_' +  FileName + '_FS_Bathy.tif')  
        FloodMapFile = os.path.join(FloodFolder, match.group(0) + '_' + FileName + '_ARC_Flood.tif')


        # these variables will have the full specifics of the streamflow source 
        ARC_FileName_FloodForecast = os.path.join(ARC_Folder, streamflow_source + '_ARC_Input_' + FileName + '_FloodForecast.txt')
        FloodDepthFile = os.path.join(FloodFolder, streamflow_source + '_' + FileName + '_ARC_FloodDepth.tif')
        FloodWSEFile = os.path.join(FloodFolder, streamflow_source + '_' + FileName + '_ARC_FloodWSE.tif') 
        FloodVELFile = os.path.join(FloodFolder, streamflow_source + '_' + FileName + '_ARC_FloodVEL.tif')
        
        #Download and Process Land Cover Data
        LandCoverFile = ''
        if not os.path.exists(LAND_File):
            (lon_1, lat_1, lon_2, lat_2, dx, dy, ncols, nrows, geoTransform, Rast_Projection) = Get_Raster_Details(DEM_File)
            
            # Get geometry in original projection
            geom = ESA.Get_Polygon_Geometry(lon_1, lat_1, lon_2, lat_2)

            # Check if raster projection is WGS 84
            raster_crs = CRS.from_wkt(Rast_Projection)
            wgs84_crs = CRS.from_epsg(4326)

            if raster_crs != wgs84_crs:
                # Convert geometry to WGS 84
                transformer = Transformer.from_crs(raster_crs, wgs84_crs, always_xy=True)
                geom = transform(transformer.transform, geom)

            LandCoverFile = ESA.Download_ESA_WorldLandCover(ESA_LC_Folder, geom, 2021)

        # This function sets-up the Input files for ARC and FloodSpreader
        # It also does some of the geospatial processing
        (ARC_FileName_Initial, ARC_FileName_Bathy, ARC_FileName_FloodForecast, Forecast_Flood_Map, 
         DEM_Reanalsyis_FlowFile, ForecastFlowFile, DEM_StrmShp, forecastdate, Forecast_Flood_Depth_Raster, 
         stream_id_field, ds_stream_id_field, flowdir_bathy) = Process_FloodForecasting_Geospatial_Data(ARC_Folder, ARC_FileName_Initial, 
                                                                                                        ARC_FileName_Bathy, ARC_FileName_FloodForecast, 
                                                                                                        DEM_File, DEM_File_Clean, LandCoverFile, 
                                                                                                        VDT_Test_File, STRM_File, 
                                                                                                        STRM_File_Clean, LAND_File, 
                                                                                                        BathyFileFolder, FloodFolder, FLOW_Folder, Flow_Direction_Folder, ManningN, 
                                                                                                        VDT_File, Curve_File, FloodMapFile, FloodDepthFile, FloodWSEFile, 
                                                                                                        FloodVELFile, FloodMapFile_Initial,
                                                                                                        DepthMapFile, ARC_BathyFile, FS_BathyFile, DEM_StrmShp, 
                                                                                                        DEM_Reanalsyis_FlowFile, bathy_use_banks, 
                                                                                                        flood_waterlc_and_strm_cells,
                                                                                                        land_watervalue, clean_dem, 
                                                                                                        use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask,
                                                                                                        find_banks_based_on_landcover, create_reach_average_curve_file,
                                                                                                        forensic_forecast_date, forensic_forecast_hour, specified_bathyflow_field, specified_highflow_field, 
                                                                                                        stream_ids_in_lake_list, streamflow_source, mapper, StrmOrder_Field, Downstream_Link_Field,
                                                                                                          StrmOrder_Lower, StrmOrder_Upper, nwm_api_key, StrmShp_gdf)  

        # if the DEM_StrmShp file is empty, then we can't do anything
        if DEM_StrmShp is None:
            LOG.info(f"Results for {DEM} are not possible because we don't have a stream shapefile...")
            return (arc_initial_simulation_time, curve2flood_initial_simulation_time, floodpsreaderpy_initial_simulation_time,
            dem_cleaner_simulation_time, 
            arc_bathy_simulation_time, curve2flood_bathy_simulation_time, floodpsreaderpy_bathy_simulation_time,
            curve2flood_forecast_simulation_time, floodpsreaderpy_forecast_simulation_time, geojson_forecast_simulation_time,
            go_consequences_simulation_time)
        
        # read in the reanalysis streamflow and break the code if the dataframe is empty or if the streamflow is all 0
        DEM_Reanalsyis_FlowFile_df = pd.read_csv(DEM_Reanalsyis_FlowFile)
        if DEM_Reanalsyis_FlowFile_df.empty is True or DEM_Reanalsyis_FlowFile_df[specified_highflow_field].mean() <= 0 or len(DEM_Reanalsyis_FlowFile_df.index)==0:
            LOG.info(f"Results for {DEM} are not possible because we don't have streamflow estimates...")
            return (arc_initial_simulation_time, curve2flood_initial_simulation_time, floodpsreaderpy_initial_simulation_time,
            dem_cleaner_simulation_time, 
            arc_bathy_simulation_time, curve2flood_bathy_simulation_time, floodpsreaderpy_bathy_simulation_time,
            curve2flood_forecast_simulation_time, floodpsreaderpy_forecast_simulation_time, geojson_forecast_simulation_time,
            go_consequences_simulation_time)

        # check and see if forecasts past a specified date exist and if so, delete them
        forecast_dir = os.path.dirname(Forecast_Flood_Map)
        forecast_file = os.path.basename(Forecast_Flood_Map)
        files_in_forecast_dir = os.listdir(forecast_dir)
        for filename in files_in_forecast_dir:
            if filename.startswith(forecast_file[:-12]):
                # Regular expression to extract the date
                date_pattern = re.compile(r'\d{8}')
                match = date_pattern.search(filename)

                if match:
                    # Extracted date string
                    date_str = match.group()
                    
                    # Convert to datetime object
                    file_date = datetime.strptime(date_str, '%Y%m%d')
                    
                    # Calculate the date 7 days ago
                    seven_days_ago = datetime.now() - timedelta(days=age_of_forecast_days)
                    
                    # Check if the file is older than 7 days
                    if file_date <= seven_days_ago:
                        # If the file is 7 days old or older, delete the file
                        if os.path.exists(filename):
                            os.remove(os.path.join(forecast_dir,filename))
                            LOG.info(f"File {filename} has been deleted.")
                        else:
                            LOG.info(f"File {filename} does not exist.")
                    else:
                        LOG.info(f"File {filename} is not old enough to be deleted.")
                else:
                    LOG.warning("No valid date found in the filename.")

        # # creat the initial flood map with the stream raster and land cover data
        if use_specified_depth_for_bathy_mask is False:
            if FloodMapFile_Initial is not None and os.path.exists(FloodMapFile_Initial) is False:
                LOG.info('Cannot find initial flood file, so creating ' + FloodMapFile_Initial)
                #Create an Initial Flood Map Based on Stream Raster and Land Cover Dataset
                Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(FloodMapFile_Initial, STRM_File_Clean, LAND_File, 80)
            else:
                LOG.info(f"{FloodMapFile_Initial} exists and we aren't making it again...")
        
        if clean_dem is True and os.path.exists(FloodMapFile_Initial) is False:
            LOG.info('Cannot find initial flood file, so creating ' + FloodMapFile_Initial)
            #Create an Initial Flood Map Based on Stream Raster and Land Cover Dataset
            Flood_WaterLC_and_STRM_Cells_in_Flood_Map_OutputTIFF(FloodMapFile_Initial, STRM_File_Clean, LAND_File, 80)

        # Run the DEM Cleaner Program, if you wanna
        if not os.path.exists(DEM_File_Clean) and clean_dem is True:
                
            Curve_File_Initial = Curve_File.replace('_CurveFile.csv','_CurveFile_Initial.csv')
            if os.path.exists(Curve_File_Initial) is False:
                # start time for the simulation
                start_time = time.time()
                arc = Arc(ARC_FileName_Initial)
                arc.run() # Runs ARC
                # end time for the simulation
                end_time = time.time()
                elapsed_time = (end_time - start_time)/60 # in minutes
                arc_initial_simulation_time = arc_initial_simulation_time + elapsed_time
            else:
                LOG.info(f"{Curve_File_Initial} exists and we aren't making it again...")
            if mapper == "FloodSpreader" and use_specified_depth_for_bathy_mask is True:
                # Resolve the path to floodspreader.py
                script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
                floodspreader_path = os.path.join(script_dir, "floodspreader.py")
                # Build the subprocess call with the full path
                start_time = time.time()
                call_mapper = f'python "{floodspreader_path}" {ARC_FileName_Initial}'
                subprocess.call(call_mapper, shell=True)
                end_time = time.time()
                elapsed_time = (end_time - start_time)/60 # in minutes 
                floodpsreaderpy_initial_simulation_time = floodpsreaderpy_initial_simulation_time + elapsed_time
            elif (mapper == "Curve2Flood" or mapper == "FLDPLN") and use_specified_depth_for_bathy_mask is True:
                # start time for the simulation
                start_time = time.time()
                LOG.info(f"Executing Curve2Flood using {ARC_FileName_Initial}")
                Curve2Flood_MainFunction(ARC_FileName_Initial)
                # end time for the simulation
                end_time = time.time()
                elapsed_time = (end_time - start_time)/60 # in minutes
                curve2flood_initial_simulation_time = curve2flood_initial_simulation_time + elapsed_time
           
            
            '''
            FloodMapFile_Initial = os.path.join(FloodFolder, FileName + '_ARC_Flood_Initial.tif')
            if not os.path.exists(FloodMapFile_Initial):
                LOG.info('Cannot find initial flood file, so creating ' + FloodMapFile_Initial)
                call_arc = 'python Automated_Rating_Curve_Generator.py ' + ARC_FileName_Initial
                subprocess.call(call_arc, shell=True)
                call_floodspreader = 'python floodspreader.py ' + ARC_FileName_Initial
                subprocess.call(call_floodspreader, shell=True)
            '''
            OutputID = 'COMID'
            Q_Fraction = 0.10
            TopWidthPlausibleLimit = 600
            search_dist_for_min_elev = 10
            search_dist_perp_cells = 10 # this was 40
            FlowFileName = os.path.join(FLOW_Folder, FileName + '_Flow_COMID_Q.txt')
            Create_FlowFile(DEM_Reanalsyis_FlowFile, FlowFileName, OutputID, 'p_exceed_50')
            # start time for the simulation
            start_time = time.time()
            DEM_Cleaner.DEM_Cleaner_Program(OutputID, 
                                            DEM_StrmShp, 
                                            DEM_Folder, 
                                            [DEM_Name], 
                                            [STRM_File_Clean], 
                                            DEM_Updated_Folder, 
                                            FlowFileName, 
                                            Curve_File.replace('_CurveFile.csv','_CurveFile_Initial.csv'), 
                                            FloodMapFile_Initial, 
                                            Q_Fraction, 
                                            TopWidthPlausibleLimit, 
                                            search_dist_for_min_elev, 
                                            search_dist_perp_cells)
            # end time for the simulation
            end_time = time.time()
            elapsed_time = (end_time - start_time)/60 # in minutes
            dem_cleaner_simulation_time = dem_cleaner_simulation_time + elapsed_time
        
        # Create a Bathymetry Raster Dataset
        if os.path.exists(FS_BathyFile) is False:
            LOG.info('Cannot find bathy file, so creating ' + FS_BathyFile)
            if os.path.exists(ARC_BathyFile) is False:
                # start time for the simulation
                start_time = time.time()
                arc = Arc(ARC_FileName_Bathy)
                arc.run() # Runs ARC
                # end time for the simulation
                end_time = time.time()
                elapsed_time = (end_time - start_time)/60 # in minutes
                arc_bathy_simulation_time = arc_bathy_simulation_time + elapsed_time
            else:
                LOG.info(f"{ARC_BathyFile} exists and we aren't making it again...")   
            if mapper == "FloodSpreader":
                # Resolve the path to floodspreader.py
                script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
                floodspreader_path = os.path.join(script_dir, "floodspreader.py")
                # Build the subprocess call with the full path
                call_mapper = f'python "{floodspreader_path}" {ARC_FileName_Bathy}'
                # start time for the simulation
                start_time = time.time()
                subprocess.call(call_mapper, shell=True)
                # end time for the simulation
                end_time = time.time()
                elapsed_time = (end_time - start_time)/60 # in minutes
                floodpsreaderpy_bathy_simulation_time = floodpsreaderpy_bathy_simulation_time + elapsed_time
            elif (mapper == "Curve2Flood" or mapper == "FLDPLN"):
                LOG.info(f"Executing Curve2Flood using {ARC_FileName_Bathy}")
                # start time for the simulation
                start_time = time.time()
                Curve2Flood_MainFunction(ARC_FileName_Bathy)
                # end time for the simulation
                end_time = time.time()
                elapsed_time = (end_time - start_time)/60 # in minutes
                curve2flood_bathy_simulation_time = curve2flood_bathy_simulation_time + elapsed_time

        else:
            LOG.info(f"{FS_BathyFile} exists and we aren't making it again...")

        
        # if the mapper is FLDPLN, then we need to remake the flood direction raster using the bathymetry output from Curve2Flood
        if mapper == "FLDPLN":
            LOG.info("Running FLDPLN to create flood direction raster...")
            FS_BathyFile_Projected = os.path.join(Flow_Direction_Folder, os.path.basename(FS_BathyFile).replace('.tif','_Projected.tif'))
            FS_BathyFile_Projected_Filled = os.path.join(Flow_Direction_Folder, os.path.basename(FS_BathyFile).replace('.tif','_Projected_Filled.tif'))
            FS_BathyFile_Projected_Filled_OriginalCRS = os.path.join(Flow_Direction_Folder, os.path.basename(FS_BathyFile).replace('.tif','_Projected_Filled_OriginalCRS.tif'))
            flowdir_projected = flowdir_bathy.replace('.tif','_Projected.tif')
            if os.path.exists(flowdir_bathy):
                LOG.info("The flow direction raster we are using to run FLDPLN already exists and we are not making it again...\n")
                pass
            else:
                Hydroterrain_Processing.create_flow_direction_raster(FS_BathyFile, BathyFileFolder, flowdir_bathy)
        
        
        # Forecast Flood Map
        # (May want to look here to do a reduced flow file to only flows that exceed a threshold.  Could also do in the forecast flow function)
        LOG.info('Creating Forecast Flood Event to be stored here: ' + Forecast_Flood_Map)
        if mapper == "FloodSpreader":
            # Resolve the path to floodspreader.py
            script_dir = os.path.dirname(os.path.abspath(__file__))  # Directory of main.py
            floodspreader_path = os.path.join(script_dir, "floodspreader.py")
            # Build the subprocess call with the full path
            call_mapper = f'python "{floodspreader_path}" {ARC_FileName_FloodForecast}'
            # start time for the simulation
            start_time = time.time()
            subprocess.call(call_mapper, shell=True)
            # end time for the simulation
            end_time = time.time()
            elapsed_time = (end_time - start_time)/60 # in minutes
            floodpsreaderpy_forecast_simulation_time = floodpsreaderpy_forecast_simulation_time + elapsed_time
        elif (mapper == "Curve2Flood" or mapper == "FLDPLN"):
            LOG.info(f"Executing Curve2Flood using {ARC_FileName_FloodForecast}")
            # start time for the simulation
            start_time = time.time()
            Curve2Flood_MainFunction(ARC_FileName_FloodForecast)
            # end time for the simulation
            end_time = time.time()
            elapsed_time = (end_time - start_time)/60 # in minutes
            curve2flood_forecast_simulation_time = curve2flood_forecast_simulation_time + elapsed_time
        LOG.info('Forecast Flood Raster saved here : ' + Forecast_Flood_Map)
        LOG.info('Forecast Flood Shapefile saved here : ' + Forecast_Flood_Map.replace('.tif','.shp'))
        # LOG.info('Forecast Flood Shapefile saved here : ' + Forecast_Flood_Map.replace('.tif','.gpkg'))

        # FIST Input Creation
        OutProjection = "EPSG:4269"
        # SEED file for creating a GEOJSON for FIST
        SEED_File = os.path.join(FIST_Folder, FileName + '_Seed.shp') 
        # ForecastFlowFile  = r"C:\Projects\2023_MultiModelFloodMapping\Yellowstone_HydroDEM\Yellowstone_flood_2022_max_streamflow_estimate.csv"
        streamflow_forecast_df = pd.read_csv(ForecastFlowFile)
        
        streamflow_columns = streamflow_forecast_df.select_dtypes(include=['float']).columns.tolist()
        VDT_File_Bathy = VDT_File.replace('.txt', '_Bathy.txt')
        for streamflow_column in streamflow_columns:
            streamflow_forecast_filtered_df = streamflow_forecast_df[['rivid', streamflow_column]]
            GeoJSON_File = os.path.join(FIST_Folder, f"{FileName}_{forecastdate}_{streamflow_column}.geojson") 
            LOG.info('Creating FIST Input: ' + GeoJSON_File)
            # start time for the simulation
            start_time = time.time()
            Run_Main_VDT_to_GEOJSON_Program_Stream_Vector(VDT_File_Bathy, STRM_File_Clean, GeoJSON_File, OutProjection, DEM_StrmShp, stream_id_field, ds_stream_id_field, SEED_File, Thin_Output=True, comid_q_df=streamflow_forecast_filtered_df)
            # end time for the simulation
            end_time = time.time()
            elapsed_time = (end_time - start_time)/60 # in minutes
            geojson_forecast_simulation_time = geojson_forecast_simulation_time + elapsed_time
    
        # now we will create the go-cosquences input GeoJSON file and run it using the Docker container
        if estimate_consequences is True:
            LOG.info("Creating the Go-Consequences JSON file and running the Go-Consequences Docker container...")
            # start time for the simulation
            start_time = time.time()   
            Forecast_Flood_Depth_Raster_Name = os.path.basename(Forecast_Flood_Depth_Raster)
            Consequences_JSON_File = Forecast_Flood_Depth_Raster_Name.replace('.tif','_consequences.json') 
            Consequences_JSON_Path = os.path.join(Consequences_Folder, Consequences_JSON_File)
            Consequences_Output_GPKG_File = Consequences_JSON_File.replace('.json','.gpkg')
            Consequences_Output_GPKG_Path = os.path.join(Consequences_Folder, Consequences_Output_GPKG_File)
            LOG.info(f"Creating consequences file {Consequences_JSON_Path}")
            Create_Go_Consequence_GeoJSON(Consequences_JSON_Path, Forecast_Flood_Depth_Raster_Name, Consequences_Output_GPKG_File)
            # run the go-consequences Docker container
            source_dir = os.path.join(Output_Dir, watershed)
            # docker run --rm --mount type=bind,source="D:\nencarta",target=/data go-consequences /data/results/streams_715_WseBanks_NotClean_Curve2Flood/consequences/USGS_13_n37w106_ARC_FloodDepth_Forecast_20250807_consequences.json
            docker_command = f'docker run --rm --mount type=bind,source="{source_dir}",target=/data go-consequences:latest /data/Consequences/{Consequences_JSON_File}'
            # run the docker command
            subprocess.call(docker_command, shell=True)
            # end time for the simulation
            end_time = time.time()
            elapsed_time = (end_time - start_time)/60 # in minutes
            go_consequences_simulation_time = go_consequences_simulation_time + elapsed_time
        else:
            LOG.info("Evidently, you don't want to run Go-Consequences, so we aren't make the consequences files...")

    return (arc_initial_simulation_time, curve2flood_initial_simulation_time, floodpsreaderpy_initial_simulation_time,
            dem_cleaner_simulation_time, 
            arc_bathy_simulation_time, curve2flood_bathy_simulation_time, floodpsreaderpy_bathy_simulation_time,
            curve2flood_forecast_simulation_time, floodpsreaderpy_forecast_simulation_time, geojson_forecast_simulation_time,
            go_consequences_simulation_time)

def process_dem(watershed_dict: dict):
    
    # set if the system will use banks of water surface elevation to estimate bathymetry
    bathy_use_banks = watershed_dict['bathy_use_banks']

    # set if you want the water-related land cover and the stream raster cells to be shown as flooding in Curve2Flood
    flood_waterlc_and_strm_cells = watershed_dict['flood_waterlc_and_strm_cells']

    # if you're using flood_waterlc_and_strm_cells, this is the raster value that indicates water in the land cover dataset
    land_watervalue = watershed_dict['land_watervalue']

    # set if floodspreader or curves2flood will be used to estimated bathymetry and flood maps
    mapper = watershed_dict['mapper']

    # set if you're using a clean DEM to process things or leave the DEM as is
    clean_dem = watershed_dict['clean_dem']

    # if you don't have the stream network preprocessed, do so
    process_stream_network = watershed_dict['process_stream_network']

    # set this to True if you want to use FloodSpreaderPy's specify depth functionality to provide a water mask for bathymetry estimation
    use_specified_depth_for_bathy_mask = watershed_dict['use_specified_depth_for_bathy_mask']

    # these are the two specify depth values that will be used to create the water mask for bathymetry estimation, if 'use_specified_depth_for_bathy_mask' is set to True 
    specify_depths_for_bathy_mask = watershed_dict['specify_depths_for_bathy_mask']

    # set the how long you want to keep the old forecasts, in days
    age_of_forecast_days = watershed_dict['age_of_forecast_days']

    # set if you want to use the landcover data to find the banks of the river, instead of the flat water surface elevation in the DEM
    find_banks_based_on_landcover = watershed_dict['find_banks_based_on_landcover']

    # let's tell ARC whether we want the curvefile parameters to be the same for each reach or vary by stream cell
    create_reach_average_curve_file = watershed_dict['create_reach_average_curve_file']

    # do you want to use the warning flags to download the DEM data you need? 
    use_warning_flags_to_download_dem = watershed_dict['use_warning_flags_to_download_dem']
    # if so, you'll need to the GEOGLOWS VPU ID
    geoglows_vpu = watershed_dict['geoglows_vpu']

    # get the forensic_forecast_date input
    forensic_forecast_date = watershed_dict['forensic_forecast_date']

    # get the forensic forecast hour input
    forensic_forecast_hour = watershed_dict['forensic_forecast_hour']

    # get the field names for the stream file that ARC will use for bathymetry and flood mapping
    specified_bathyflow_field = watershed_dict['specified_bathyflow_field']
    specified_highflow_field = watershed_dict['specified_highflow_field']
    StrmOrder_Field = watershed_dict.get('StrmOrder_Field')
    Downstream_Link_Field = watershed_dict.get('Downstream_Link_Field')
    StrmOrder_Lower = watershed_dict.get('StrmOrder_Lower')
    StrmOrder_Upper = watershed_dict.get('StrmOrder_Upper')

    # get the lake_filter_json and if it exists read it in
    lake_filter_json = watershed_dict.get('lake_filter_json', None)
    if lake_filter_json is not None:
        with open(lake_filter_json, 'r') as f:
            lake_filter = json.load(f)
            stream_ids_in_lake_list = []
            for _k, v in lake_filter.items():
                inside = v.get("inside", [])
                for x in inside:
                    if x is not None:
                        stream_ids_in_lake_list.append(x)
    else:
        stream_ids_in_lake_list = None

    # boolean to determine if we are going to estimate consequences using go-consequences
    estimate_consequences = watershed_dict['estimate_consequences']
    LOG.info(f"Estimate consequences is set to {estimate_consequences}")

    # string to describe what the source of the streamflow data is
    streamflow_source = watershed_dict['streamflow_source']

    # API key for NWM requests (required when streamflow_source is NWM)
    nwm_api_key = watershed_dict.get('nwm_api_key')
    if streamflow_source.upper().startswith("NWM") and not nwm_api_key:
        raise ValueError("nwm_api_key is required when streamflow_source is NWM.")



    #Folder Management
    Output_Dir = watershed_dict['output_dir']
    watershed = watershed_dict['name']
    DEM_Folder = watershed_dict['dem_dir']
    ARC_Folder = os.path.join(Output_Dir, watershed, 'ARC_InputFiles')
    FloodFolder = os.path.join(Output_Dir, watershed, 'FloodMap')
    BathyFileFolder = os.path.join(Output_Dir, watershed, 'Bathymetry')
    DEM_Updated_Folder = os.path.join(Output_Dir, watershed, 'DEM_Updated')
    STRM_Folder = os.path.join(Output_Dir, watershed, 'STRM')
    LAND_Folder = os.path.join(Output_Dir, watershed, 'LAND')
    FLOW_Folder = os.path.join(Output_Dir, watershed, 'FLOW')
    VDT_Folder = os.path.join(Output_Dir, watershed, 'VDT')
    ESA_LC_Folder = os.path.join(Output_Dir, watershed, 'ESA_LC')
    FIST_Folder = os.path.join(Output_Dir, watershed, 'FIST')
    Consequences_Folder = os.path.join(Output_Dir, watershed, 'Consequences')
    Flow_Direction_Folder = os.path.join(Output_Dir, watershed, 'FlowDirection')

    # Validate data
    if mapper not in ["FloodSpreader", "Curve2Flood", "FLDPLN"]:
        raise ValueError("Invalid mapper specified. Choose 'FloodSpreader', 'Curve2Flood', or 'FLDPLN'.")
    
    if mapper == 'FLDPLN':
        if not all([StrmOrder_Field, Downstream_Link_Field]):
            raise ValueError("StrmOrder_Field and Downstream_Link_Field must be specified when using 'FLDPLN' mapper.")
    
    #Create Folders
    # Create_Folder(watershed)
    Create_Folder(ESA_LC_Folder)
    Create_Folder(STRM_Folder)
    Create_Folder(LAND_Folder)
    Create_Folder(FLOW_Folder)
    Create_Folder(VDT_Folder)
    Create_Folder(DEM_Updated_Folder)
    Create_Folder(FloodFolder)
    Create_Folder(ARC_Folder)
    Create_Folder(BathyFileFolder)
    Create_Folder(FIST_Folder)
    Create_Folder(Consequences_Folder)
    Create_Folder(Flow_Direction_Folder)
    
    #Datasets that can be good for a large domain
    StrmSHP = watershed_dict['flowline']
    ManningN = os.path.join(LAND_Folder, 'AR_Manning_n_MED.txt')

    #Create a Baseline Manning N File
    LOG.info('Creating Manning n file: ' + ManningN)
    Create_BaseLine_Manning_n_File_ESA(ManningN)

    # If you are using the warning flags to download the DEM data, do it now
    if use_warning_flags_to_download_dem == True:
        # This outputs a list of DEMs were GEOGLOWS has forecasted flooding (2-year exceedance or above)
        DEM_List = ForecastFlows.Download_USGS_DEM_Data_Using_WarningFlag_Data(geoglows_vpu, DEM_Folder, forensic_forecast_date)
    else:
        #This is the list of all the DEM files we will go through
        DEM_List = os.listdir(DEM_Folder)
    
    # if DEM List is empty, then we need to break out of the function
    if len(DEM_List) == 0:
        LOG.info("No DEMs found in the specified folder.")
        return

    # Before we get too far ahead, let's make sure that our DEMs and Flowlines have the same coordinate system
    # we will assume that all DEMs in the DEM list have the same coordinate system
    if process_stream_network is True:
        LOG.info('Reading in stream file: ' + StrmSHP)
        if StrmSHP.endswith(".gdb"):
            # Specify the layer you want to access
            layer_name = "geoglowsv2"
            # Read the layer from the geodatabase
            StrmShp_gdf = gpd.read_file(StrmSHP, layer=layer_name)    
        elif StrmSHP.endswith(".shp") or StrmSHP.endswith(".gpkg"):
            # Read the layer from the shapefile
            StrmShp_gdf = gpd.read_file(StrmSHP)
        elif StrmSHP.endswith(".parquet"):
            # Read the layer from the shapefile
            StrmShp_gdf = gpd.read_parquet(StrmSHP)

        # removing any lingering NoneType geometries
        StrmShp_gdf = StrmShp_gdf[~StrmShp_gdf.geometry.isna()]

        LOG.info('Converting the coordinate system of the stream file to match the DEM files, if necessary')
        test_dem = next((file for file in DEM_List if file.endswith('.tif')), None)
        test_dem_path = os.path.join(DEM_Folder,test_dem)
        # Load the DEM file and get its CRS using gdal
        dem_dataset = gdal.Open(test_dem_path)
        dem_proj = dem_dataset.GetProjection()  # Get the projection as a WKT string
        dem_spatial_ref = osr.SpatialReference()
        dem_spatial_ref.ImportFromWkt(dem_proj)
        # dem_crs = dem_spatial_ref.ExportToProj4()  # Export CRS to a Proj4 string (or other formats if needed)
        # Get the EPSG code
        dem_spatial_ref.AutoIdentifyEPSG()
        dem_epsg_code = dem_spatial_ref.GetAuthorityCode(None)  # This extracts the EPSG code as a string
        # Check if the CRS of the shapefile matches the DEM's CRS
        if int(str(StrmShp_gdf.crs)[5:]) != int(dem_epsg_code):
            LOG.info("DEM and Stream Network have different coordinate systems...")
            LOG.info(f"Stream CRS: {str(StrmShp_gdf.crs)[5:]}")
            LOG.info(f"DEM CRS: {dem_epsg_code}")
            # Reproject the shapefile to match the DEM's CRS
            StrmShp_gdf = StrmShp_gdf.to_crs(dem_epsg_code)
        dem_dataset = None
        dem_proj = None 
        dem_spatial_ref = None
    elif process_stream_network is False:
        StrmShp_gdf = None   
    
    arc_initial_simulation_time = 0.0
    curve2flood_initial_simulation_time = 0.0
    floodpsreaderpy_initial_simulation_time = 0.0
    dem_cleaner_simulation_time = 0.0
    arc_bathy_simulation_time = 0.0
    curve2flood_bathy_simulation_time = 0.0
    floodpsreaderpy_bathy_simulation_time = 0.0
    curve2flood_forecast_simulation_time = 0.0
    floodpsreaderpy_forecast_simulation_time = 0.0
    geojson_forecast_simulation_time = 0.0
    go_consequences_simulation_time = 0.0

    #Now go through each DEM dataset
    for DEM in DEM_List:

        (arc_initial_simulation_time, curve2flood_initial_simulation_time, floodpsreaderpy_initial_simulation_time,
        dem_cleaner_simulation_time, arc_bathy_simulation_time, curve2flood_bathy_simulation_time, floodpsreaderpy_bathy_simulation_time,
        curve2flood_forecast_simulation_time, floodpsreaderpy_forecast_simulation_time, geojson_forecast_simulation_time, go_consequences_simulation_time) = DEM_Forecast(DEM_Folder, DEM, Output_Dir, watershed, ESA_LC_Folder, STRM_Folder, LAND_Folder, FLOW_Folder, VDT_Folder, DEM_Updated_Folder, FloodFolder, ARC_Folder, 
                                                                                                                                            BathyFileFolder, FIST_Folder, Flow_Direction_Folder, ManningN, bathy_use_banks, flood_waterlc_and_strm_cells, land_watervalue, mapper, clean_dem, 
                                                                                                                                            use_specified_depth_for_bathy_mask, specify_depths_for_bathy_mask, age_of_forecast_days, find_banks_based_on_landcover, create_reach_average_curve_file,
                                                                                                                                            arc_initial_simulation_time, curve2flood_initial_simulation_time, floodpsreaderpy_initial_simulation_time,
                                                                                                                                            dem_cleaner_simulation_time,
                                                                                                                                            arc_bathy_simulation_time, curve2flood_bathy_simulation_time, floodpsreaderpy_bathy_simulation_time, 
                                                                                                                                            curve2flood_forecast_simulation_time, floodpsreaderpy_forecast_simulation_time, geojson_forecast_simulation_time,
                                                                                                                                            estimate_consequences, go_consequences_simulation_time,
                                                                                                                                            forensic_forecast_date, forensic_forecast_hour, specified_bathyflow_field, specified_highflow_field, stream_ids_in_lake_list, 
                                                                                                                                            Consequences_Folder, streamflow_source, StrmOrder_Field, Downstream_Link_Field,
                                                                                                                                            StrmOrder_Lower, StrmOrder_Upper, nwm_api_key, StrmShp_gdf)
    
    # delete the ESA_LC_Folder and the data in it
    # Loop through all files in the directory and remove them
    for file in Path(ESA_LC_Folder).glob("*"):
        try:
            if file.is_file():
                # Adjust file permissions before deletion
                if platform.system() == "Windows":
                    os.chmod(file, stat.S_IWRITE)  # Remove read-only attribute on Windows
                else:
                    os.chmod(file, stat.S_IWUSR)   # Give user write permission on Unix systems
                os.remove(file)
                LOG.info(f"process_dam: Deleted file: {file}")
        except Exception as e:
            LOG.error(f"Error deleting file {file}: {e}")
    if os.path.exists(ESA_LC_Folder):
        # Adjust file permissions before deletion
        if platform.system() == "Windows":
            os.chmod(ESA_LC_Folder, stat.S_IWRITE)  # Remove read-only attribute on Windows
        else:
            os.chmod(ESA_LC_Folder, stat.S_IWUSR)   # Give user write permission on Unix systems
        os.rmdir(ESA_LC_Folder)
        LOG.info(f"process_dam: Deleted empty folder: {ESA_LC_Folder}")
    else:
        LOG.info(f"process_dam: Folder {ESA_LC_Folder} does not exist.")
    
    # here are the simulation times for each of the processes for the watershed
    LOG.info(f"Here are the simulation times for each of the processes for the watershed {watershed}:")
    LOG.info(f"ARC Initial Flood Simulation Time: {arc_initial_simulation_time} minutes")
    LOG.info(f"Curve2Flood Initial Flood Simulation Time: {curve2flood_initial_simulation_time} minutes")
    LOG.info(f"FloodSpreaderPy Initial Flood Simulation Time: {floodpsreaderpy_initial_simulation_time} minutes")
    LOG.info(f"DEM Cleaner Simulation Time: {dem_cleaner_simulation_time} minutes")
    LOG.info(f"ARC Bathymetry Simulation Time: {arc_bathy_simulation_time} minutes")
    LOG.info(f"Curve2Flood Bathymetry Simulation Time: {curve2flood_bathy_simulation_time} minutes")
    LOG.info(f"FloodSpreaderPy Bathymetry Simulation Time: {floodpsreaderpy_bathy_simulation_time} minutes")
    LOG.info(f"Curve2Flood Forecast Simulation Time: {curve2flood_forecast_simulation_time} minutes")
    LOG.info(f"FloodSpreaderPy Forecast Simulation Time: {floodpsreaderpy_forecast_simulation_time} minutes")
    LOG.info(f"GeoJSON Forecast Simulation Time: {geojson_forecast_simulation_time} minutes")
    LOG.info(f"Go-Consequences Simulation Time: {go_consequences_simulation_time} minutes")
    
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
