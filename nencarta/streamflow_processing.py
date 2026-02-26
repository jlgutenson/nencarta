#Code written by Mike Follum to try and evaluate the mean flow from GEOGLOWS datasets.
#GEOGLOWS data can be downloaded from http://geoglows-v2.s3-website-us-west-2.amazonaws.com/

# built-in imports
import gc
import os
import json

# third-party imports
import dask.dataframe as dd
from dask.diagnostics import ProgressBar
import geopandas as gpd
import numpy as np
from osgeo import gdal
import pandas as pd
import requests
import io
from shapely.geometry import box
import s3fs
import xarray as xr

from .flood_folder import FloodFolder
from .logger import LOG

_RP_DS: xr.Dataset = None
_FDC_DS: xr.Dataset = None
_DAILY_DS: xr.Dataset = None

def get_rp_ds():
    """ This is faster for multiprocessing contexts since the dataset is only loaded once per process."""
    global _RP_DS
    if _RP_DS is None:
        _RP_DS = xr.open_zarr('s3://geoglows-v2/retrospective/return-periods.zarr', storage_options={'anon': True})

    return _RP_DS

def get_fdc_ds():
    global _FDC_DS
    if _FDC_DS is None:
        _FDC_DS = xr.open_zarr('s3://geoglows-v2/retrospective/fdc.zarr', storage_options={'anon': True})

    return _FDC_DS

def get_daily_ds():
    global _DAILY_DS
    if _DAILY_DS is None:
        _DAILY_DS = xr.open_zarr('s3://geoglows-v2/retrospective/daily.zarr', storage_options={'anon': True})

    return _DAILY_DS

def get_nwm_rp(comids: list[int], nwm_api_key: str):
    rp_url = 'https://nwm-api.ciroh.org/return-period'

    if not nwm_api_key:
        raise ValueError("nwm_api_key is required for NWM return period requests.")

    header = {'x-api-key': nwm_api_key}
    params = {'comids': ','.join(map(str, comids)),
              'output_format': 'csv',
              'order_by_comid': False,}

    response = requests.get(rp_url, params=params, headers=header, timeout=60)

    if response.status_code == 200:
        return_period_df = pd.read_csv(io.StringIO(response.text))
    else:
        raise requests.exceptions.HTTPError(response.text)
    return_period_df = return_period_df.set_index("feature_id")
    return_period_df.index.name = "river_id"
    return_period_df.columns = ['rp2', 'rp5', 'rp10', 'rp25', 'rp50', 'rp100']

    return return_period_df

def GetMeanFlowValues(NetCDF_Directory):
    """
    Estimates the mean streamflow for all stream reaches by cycling through a directory of yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files, 
    estimating the yearly mean, and then estimating a mean of those yearly means

    Parameters
    ----------
    NetCDF_Directory: str
        The file path to a directory containing yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files
    
    Returns
    -------
    overall_mean_Qout: Pandas series
        A Pandas series of mean streamflow values with the streams unique identifier used as the index

    """
    # create a list of all files in the NetCDF directory
    file_list = os.listdir(NetCDF_Directory)
    all_mean_Qout_dfs = []
    for f in file_list:
        if f.endswith(".nc"):
            qout_file_path = os.path.join(NetCDF_Directory, f)
            qout_ds = xr.open_dataset(qout_file_path, engine='netcdf4')
            
            # Compute the mean and Qout value over the 'time' dimension for each rivid
            mean_Qout_all_rivids = qout_ds['Qout'].mean(dim='time')

            # Trigger the computation if using Dask (although not necessary here since the dataset is 335MB)
            mean_Qout_all_rivids_values = mean_Qout_all_rivids.compute()

            # Convert the xarray DataArray to a pandas DataFrame
            mean_Qout_df = mean_Qout_all_rivids.to_dataframe(name='qout_mean').reset_index()
            
            all_mean_Qout_dfs.append(mean_Qout_df)
            
    # Concatenate all DataFrames into a single DataFrame
    if all_mean_Qout_dfs:
        all_mean_Qout_df = pd.concat(all_mean_Qout_dfs, ignore_index=True).round(3)
    else:
        LOG.error("No valid data found in the NetCDF files.")
        return None
    
    # Compute overall average by rivid
    overall_mean_Qout = all_mean_Qout_df.groupby('rivid')['qout_mean'].mean().round(3)
        
    return (overall_mean_Qout)

def GetMedianFlowValues(NetCDF_Directory):
    """
    Estimates the median streamflow for all stream reaches by cycling through a directory of yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files, 
    estimating the yearly median, and then estimating a median of those yearly medians

    Parameters
    ----------
    NetCDF_Directory: str
        The file path to a directory containing yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files
    
    Returns
    -------
    overall_median_Qout: Pandas series
        A Pandas series of median streamflow values with the streams unique identifier used as the index

    """
    # Create a list of all files in the NetCDF directory
    file_list = os.listdir(NetCDF_Directory)
    all_median_Qout_dfs = []
    
    for f in file_list:
        if f.endswith(".nc"):
            qout_file_path = os.path.join(NetCDF_Directory, f)
            qout_ds = xr.open_dataset(qout_file_path, engine='netcdf4')
            
            # Compute the median Qout value over the 'time' dimension for each rivid
            median_Qout_all_rivids = qout_ds['Qout'].median(dim='time')

            # Trigger the computation if using Dask (although not necessary here since the dataset is 335MB)
            median_Qout_all_rivids_values = median_Qout_all_rivids.compute()

            # Convert the xarray DataArray to a pandas DataFrame
            median_Qout_df = median_Qout_all_rivids.to_dataframe(name='qout_median').reset_index()
            
            all_median_Qout_dfs.append(median_Qout_df)
            
    # Concatenate all DataFrames into a single DataFrame
    if all_median_Qout_dfs:
        all_median_Qout_df = pd.concat(all_median_Qout_dfs, ignore_index=True).round(3)
    else:
        LOG.error("No valid data found in the NetCDF files.")
        return None
    
    # Compute overall median by rivid
    overall_median_Qout = all_median_Qout_df.groupby('rivid')['qout_median'].median().round(3)
        
    return overall_median_Qout

def GetMaxFlowValues(NetCDF_Directory):
    """
    Estimates the maximum streamflow for all stream reaches by cycling through a directory of yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files, 
    estimating the yearly maximum, and then estimating a maximum of those yearly maximums

    Parameters
    ----------
    NetCDF_Directory: str
        The file path to a directory containing yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files
    
    Returns
    -------
    overall_median_Qout: Pandas series
        A Pandas series of maximum streamflow values with the streams unique identifier used as the index

    """
    # create a list of all files in the NetCDF directory
    file_list = os.listdir(NetCDF_Directory)
    all_max_Qout_dfs = []
    for f in file_list:
        if f.endswith(".nc"):
            qout_file_path = os.path.join(NetCDF_Directory, f)
            qout_ds = xr.open_dataset(qout_file_path, engine='netcdf4')
            
            # Compute the max Qout value over the 'time' dimension for each rivid
            max_Qout_all_rivids = qout_ds['Qout'].max(dim='time')

            # Trigger the computation if using Dask (although not necessary here since the dataset is 335MB)
            max_Qout_all_rivids_values = max_Qout_all_rivids.compute()

            # Convert the xarray DataArray to a pandas DataFrame
            max_Qout_df = max_Qout_all_rivids.to_dataframe(name='qout_max').reset_index()
            
            all_max_Qout_dfs.append(max_Qout_df)
            
    # Concatenate all DataFrames into a single DataFrame
    if all_max_Qout_dfs:
        all_max_Qout_df = pd.concat(all_max_Qout_dfs, ignore_index=True).round(3)
    else:
        LOG.error("No valid data found in the NetCDF files.")
        return None
    
    # Compute overall average by rivid
    overall_max_Qout = all_max_Qout_df.groupby('rivid')['qout_max'].max().round(3)
        
    return (overall_max_Qout)

def GetReturnPeriodFlowValues(NetCDF_File_Path):
    """
    Estimates the maximum streamflow for all stream reaches by cycling through a directory of yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files, 
    estimating the yearly maximum, and then estimating a maximum of those yearly maximums

    Parameters
    ----------
    NetCDF_File_Path: str
        The file path and file name of a NetCDF of recurrence interval streamflow file from the GEOGLOWS ECMWF Streamflow Service
    
    Returns
    -------
    qout_df: Pandas dataframe
        A Pandas dataframe of the recurrence interval values contained in the recurrence interval streamflow file from the GEOGLOWS ECMWF Streamflow Service

    """
    # Open the NetCDF with xarray
    qout_ds = xr.open_dataset(NetCDF_File_Path, engine='netcdf4')
            
    # Convert xarray Dataset to pandas DataFrame
    qout_df = qout_ds.to_dataframe()
            
    return (qout_df)

def Create_ARC_Streamflow_Input(NetCDF_RecurrenceInterval_File_Path, NetCDF_Historical_Folder, Outfile_file_path):
    """
    Creates a streamflow input file that can be used by the Automated Rating Curve (ARC) tool

    Parameters
    ----------
    NetCDF_RecurrenceInterval_File_Path: str
        The file path and file name of a NetCDF of recurrence interval streamflow file from the GEOGLOWS ECMWF Streamflow Service
    NetCDF_Historical_Folder: str
        The file path to a directory containing yearly retrospective GEOGLOWS ECMWF Streamflow Service NetCDF files
    Outfile_file_path: str
        The file path and file name of the file that will store the resulting streamflow inputs for ARC
    
    Returns
    -------
    combined_df: Pandas dataframe
        A Pandas dataframe of the mean, median, maximum, 2-year recurrence interval, 5-year recurrence interval, 10-year recurrence interval, 25-year recurrence interval,
        50-year recurrence interval, and 100-year recurrence interval streamflow values contained in the recurrence interval streamflow file 
        from the GEOGLOWS ECMWF Streamflow Service

    """
    overall_median_Qout = GetMedianFlowValues(NetCDF_Historical_Folder)
    overall_median_Qout = abs(overall_median_Qout)
    overall_mean_Qout = GetMeanFlowValues(NetCDF_Historical_Folder)
    combined_df = GetReturnPeriodFlowValues(NetCDF_RecurrenceInterval_File_Path)
    
    # Append Series to DataFrame using .loc indexer
    combined_df.loc[:, overall_mean_Qout.name] = overall_mean_Qout
    combined_df.loc[:, overall_median_Qout.name] = overall_median_Qout
    
    combined_df['COMID'] = combined_df.index
    
    # Define custom order of columns
    custom_order = ['COMID','qout_mean','qout_median','qout_max','rp2','rp5','rp10','rp25','rp50','rp100']
    
    # Sort columns in custom order
    combined_df = combined_df[custom_order]
    
    # Output the combined Dataframe as a CSV
    combined_df.to_csv(Outfile_file_path,index=False)
    
    return (combined_df)

def Process_and_Write_Retrospective_Data(StrmShp_gdf, rivid_field, CSV_File_Name):
    rivids = StrmShp_gdf[rivid_field].astype(int).values

    # Set up the S3 connection
    ODP_S3_BUCKET_REGION = 'us-west-2'
    s3 = s3fs.S3FileSystem(anon=True, client_kwargs=dict(region_name=ODP_S3_BUCKET_REGION))

    # Enable Dask progress bar
    with ProgressBar():
    
        # Load retrospective data from S3 using Dask
        retro_s3_uri = 's3://geoglows-v2-retrospective/retrospective.zarr'
        retro_s3store = s3fs.S3Map(root=retro_s3_uri, s3=s3, check=False)
        retro_ds = xr.open_zarr(retro_s3store, chunks='auto').sel(rivid=rivids)
        
        # Convert Xarray to Dask DataFrame
        retro_ddf = retro_ds.to_dask_dataframe().reset_index()

        # Perform groupby operations in Dask for mean, median, and max
        mean_ddf = retro_ddf.groupby('rivid').Qout.mean().rename('qout_mean').reset_index()
        median_ddf = retro_ddf.groupby('rivid').Qout.median().rename('qout_median').reset_index()
        max_ddf = retro_ddf.groupby('rivid').Qout.max().rename('qout_max').reset_index()

        # Set the index for alignment and repartition
        mean_ddf = mean_ddf.set_index('rivid')
        median_ddf = median_ddf.set_index('rivid')
        max_ddf = max_ddf.set_index('rivid')

        # Repartition to align the partitions
        mean_ddf = mean_ddf.repartition(npartitions=10)
        median_ddf = median_ddf.repartition(npartitions=10)
        max_ddf = max_ddf.repartition(npartitions=10)

        # Align partitions
        combined_ddf = dd.concat([
            mean_ddf,
            median_ddf,
            max_ddf
        ], axis=1)

    # Clean up memory
    del retro_ds, retro_ddf, mean_ddf, median_ddf, max_ddf
    gc.collect()

    # Enable Dask progress bar
    with ProgressBar():
    
        # Load return periods data from S3 using Dask
        rp_s3_uri = 's3://geoglows-v2-retrospective/return-periods.zarr'
        rp_s3store = s3fs.S3Map(root=rp_s3_uri, s3=s3, check=False)
        rp_ds = xr.open_zarr(rp_s3store, chunks='auto').sel(rivid=rivids)
        
        # Convert Xarray to Dask DataFrame and pivot
        rp_ddf = rp_ds.to_dask_dataframe().reset_index()

        # Convert 'return_period' to category dtype
        rp_ddf['return_period'] = rp_ddf['return_period'].astype('category')

        # Ensure the categories are known
        rp_ddf['return_period'] = rp_ddf['return_period'].cat.as_known()
        
        # Pivot the table
        rp_pivot_ddf = rp_ddf.pivot_table(index='rivid', columns='return_period', values='return_period_flow', aggfunc='mean')

        # Rename columns to indicate return periods
        rp_pivot_ddf = rp_pivot_ddf.rename(columns={col: f'rp{int(col)}' for col in rp_pivot_ddf.columns})

        # Set the index for rp_pivot_ddf and ensure known divisions
        rp_pivot_ddf = rp_pivot_ddf.reset_index().set_index('rivid').repartition(npartitions=rp_pivot_ddf.npartitions)
        rp_pivot_ddf = rp_pivot_ddf.set_index('rivid', sorted=True)

    # Clean up memory
    del rp_ds, rp_ddf
    gc.collect()
    
    # # Align partitions
    # aligned_dfs, divisions, result = dd.multi.align_partitions(combined_ddf, rp_pivot_ddf)

    # # Extract aligned DataFrames
    # aligned_combined_ddf = aligned_dfs[0]
    # aligned_rp_pivot_ddf = aligned_dfs[1]

    # Repartition to align the partitions
    aligned_combined_ddf = combined_ddf.repartition(npartitions=10)
    aligned_rp_pivot_ddf = rp_pivot_ddf.repartition(npartitions=10)

    # Combine the results from retrospective and return periods data
    final_ddf = dd.concat([aligned_combined_ddf, aligned_rp_pivot_ddf], axis=1)

    # Write the final Dask DataFrame to CSV
    final_ddf.to_csv(CSV_File_Name, single_file=True, index=False)

    # Clean up memory
    del rp_pivot_ddf, combined_ddf, final_ddf
    gc.collect()
    
    # Return the combined DataFrame as a Dask DataFrame
    return

class PatchedZarrStore(dict):
    def __init__(self, base_store, zmetadata_bytes):
        self.base_store = base_store
        self.zmetadata_bytes = zmetadata_bytes

    def __getitem__(self, key):
        if key == ".zmetadata":
            return self.zmetadata_bytes
        return self.base_store[key]

    def __iter__(self):
        # Add ".zmetadata" to the key list if not already present
        for key in self.base_store:
            yield key
        if ".zmetadata" not in self.base_store:
            yield ".zmetadata"

    def __len__(self):
        return len(set(self.base_store.keys()) | {".zmetadata"})

    def keys(self):
        return list(self.__iter__())


def Process_and_Write_Retrospective_Data_for_DEM_Tile(StrmShp_gdf: gpd.GeoDataFrame, rivid_field, folder: FloodFolder, watershed_dict: dict):

    # First let's remove the stream reaches that are in the stream_ids_in_lake_list
    # filter out the streams that are in the stream_ids_in_lake_list by using the "LINKNO values in stream_ids_in_lake_list"
    lake_filter_json = watershed_dict.get('lake_filter_json', None)
    if lake_filter_json:
        with open(lake_filter_json, 'r') as f:
            lake_filter: dict = json.load(f)
            stream_ids_in_lake_list = []
            for _k, v in lake_filter.items():
                inside = v.get("inside", [])
                for x in inside:
                    if x is not None:
                        stream_ids_in_lake_list.append(x)
        StrmShp_gdf = StrmShp_gdf[~StrmShp_gdf[rivid_field].isin(stream_ids_in_lake_list)]

    # if the StrmOrder_Field and StrmOrder_Lower or StrmOrder_Upper are not None use these to filter the StrmShp_gdf
    if watershed_dict.get("StrmOrder_Field") and (watershed_dict.get("StrmOrder_Lower") is not None or watershed_dict.get("StrmOrder_Upper") is not None):
        if watershed_dict["StrmOrder_Field"] not in StrmShp_gdf.columns:
            LOG.warning(f"StrmOrder_Field '{watershed_dict['StrmOrder_Field']}' not found in stream shapefile; skipping stream order filter.")
        else:
            order_vals = pd.to_numeric(StrmShp_gdf[watershed_dict["StrmOrder_Field"]], errors="coerce")
            mask = order_vals.notna()
            if watershed_dict.get("StrmOrder_Lower"):
                mask &= order_vals >= watershed_dict["StrmOrder_Lower"]
            if watershed_dict.get("StrmOrder_Upper"):
                mask &= order_vals <= watershed_dict["StrmOrder_Upper"]
            StrmShp_gdf = StrmShp_gdf.loc[mask].copy()

    # Load the raster tile and get its bounds using gdal
    raster_dataset = gdal.Open(folder.DEM_File)
    gt = raster_dataset.GetGeoTransform()

    # # Get the bounds of the raster (xmin, ymin, xmax, ymax)
    # xmin = gt[0]
    # xmax = xmin + gt[1] * raster_dataset.RasterXSize
    # ymin = gt[3] + gt[5] * raster_dataset.RasterYSize
    # ymax = gt[3]

    # # Create a bounding box
    # raster_bbox = box(xmin, ymin, xmax, ymax)

    # Read the raster band
    band = raster_dataset.GetRasterBand(1)
    array = band.ReadAsArray()

    # Identify valid data mask (non-NaN, and optionally not equal to NoData value)
    nodata_value = band.GetNoDataValue()
    if nodata_value is not None:
        valid_mask = (array != nodata_value) & ~np.isnan(array)
    else:
        valid_mask = ~np.isnan(array)

    # Check for at least some valid data
    if not np.any(valid_mask):
        LOG.error(f"No valid data found in DEM tile: {folder.DEM_File}")
        return (None, None)

    # Get pixel indices of valid data
    rows = np.any(valid_mask, axis=1)
    cols = np.any(valid_mask, axis=0)

    y_valid_min, y_valid_max = np.where(rows)[0][[0, -1]]
    x_valid_min, x_valid_max = np.where(cols)[0][[0, -1]]

    # Convert pixel indices to geocoordinates using affine transform
    x_min_valid = gt[0] + x_valid_min * gt[1]
    x_max_valid = gt[0] + (x_valid_max + 1) * gt[1]
    y_max_valid = gt[3] + y_valid_min * gt[5]
    y_min_valid = gt[3] + (y_valid_max + 1) * gt[5]

    # Create a refined bounding box
    raster_bbox = box(x_min_valid, y_min_valid, x_max_valid, y_max_valid)

    # Use GeoPandas spatial index to quickly find geometries within the bounding box
    sindex = StrmShp_gdf.sindex
    possible_matches_index = list(sindex.intersection(raster_bbox.bounds))
    # Mike thinks that the "StrmShp_gdf.iloc[possible_matches_index]" function works just fine without the "possible_matches[possible_matches.geometry.within(raster_bbox)]" function
    '''
    possible_matches = StrmShp_gdf.iloc[possible_matches_index]

    # Collect IDs of polyline features within the raster tile boundary
    StrmShp_filtered_gdf = possible_matches[possible_matches.geometry.within(raster_bbox)]

    # First attempt at fixing an empty StrmShp_filtered_gdf
    if StrmShp_filtered_gdf.empty:
        StrmShp_filtered_gdf = StrmShp_gdf.iloc[possible_matches_index]
    '''
    StrmShp_filtered_gdf = StrmShp_gdf.iloc[possible_matches_index]

    # ensure that a 'LINKNO' and 'COMID' fields exists in StrmShp_filtered_gdf
    if 'LINKNO' not in StrmShp_filtered_gdf.columns:
        StrmShp_filtered_gdf['LINKNO'] = StrmShp_filtered_gdf[rivid_field]
    if 'COMID' not in StrmShp_filtered_gdf.columns:
        StrmShp_filtered_gdf['COMID'] = StrmShp_filtered_gdf[rivid_field]

    # Second attempt at fixing an empty StrmShp_filtered_gdf
    if StrmShp_filtered_gdf.empty:
        LOG.warning(f"Skipping processing for {folder.DEM_File} because StrmShp_filtered_gdf is empty.")
        return (None, None)

    StrmShp_filtered_gdf.to_file(folder.DEM_StrmShp, driver="GPKG")
    StrmShp_filtered_gdf[rivid_field] = StrmShp_filtered_gdf[rivid_field].astype(int)

    # create a list of river IDs to throw to AWS
    rivids_str = StrmShp_filtered_gdf[rivid_field].astype(str).to_list()
    rivids_int = StrmShp_filtered_gdf[rivid_field].astype(int).to_list()

    # Column to move to the front
    target_column = 'COMID'

    if rivid_field == 'LINKNO':

        # Get the GEOGLOWS return period dataset and select the river IDs of interest
        rp_ds = get_rp_ds().sel(river_id=rivids_int)
        
        # Convert Xarray to Dask DataFrame and pivot
        rp_df = rp_ds.to_dataframe().reset_index()

        # find the maximum between the gumbel and logpearson3 return periods and label this new column 'return_period_flow'
        rp_df['return_period_flow'] = rp_df[['gumbel', 'logpearson3']].mean(axis=1).round(3)

        # keep just the column 'return_period_flow'
        rp_df = rp_df[['river_id', 'return_period', 'return_period_flow']]
        
        # Check if rp_df is empty
        if rp_df.empty:
            LOG.warning(f"Skipping processing for {folder.DEM_File} because rp_df is empty.")
            CSV_File_Name = None
            OutShp_File_Name = None
            rivids_int = None
            StrmShp_filtered_gdf = None
            return (CSV_File_Name, OutShp_File_Name, rivids_int, StrmShp_filtered_gdf)

        # Convert 'return_period' to category dtype
        rp_df['return_period'] = rp_df['return_period'].astype('category')
        
        # Pivot the table
        rp_pivot_df = rp_df.pivot_table(index='river_id', columns='return_period', values='return_period_flow', aggfunc='mean', observed=False)

        # Rename columns to indicate return periods
        rp_pivot_df = rp_pivot_df.rename(columns={col: f'rp{int(col)}' for col in rp_pivot_df.columns})

        def _format_p_exceed_label(value):
            try:
                label = f"{float(value):g}"
            except (TypeError, ValueError):
                label = str(value)
            return label.replace(".", "_")

        # try to maximum and mean flows a couple of different ways
        try:
            # # Load FDC data from S3 using Dask
            # # Convert to a list of integers
            p_exceedance = [float(v) for v in range(5, 101, 5)]
            p_exceedance.insert(0, 0.0)
            fdc_ds = get_fdc_ds()
            available_p_exceed = [float(v) for v in fdc_ds["p_exceed"].values.tolist()]
            p_exceedance = [v for v in p_exceedance if v in available_p_exceed]
            if not p_exceedance:
                raise ValueError("No requested p_exceedance values were found in the FDC dataset.")
            fdc_ds = fdc_ds.sel(p_exceed=p_exceedance, river_id=rivids_int)


            # Convert Xarray to Dask DataFrame
            fdc_df = fdc_ds.to_dataframe().reset_index()


            # Check if fdc_df is empty
            if fdc_df.empty:
                LOG.warning(f"Skipping processing for {folder.DEM_File} because fdc_df is empty.")
                return (None, None)

            fdc_pivot = fdc_df.pivot_table(
                index='river_id',
                columns='p_exceed',
                values='hourly_annual',
                aggfunc='mean'
            )
            fdc_pivot = fdc_pivot.rename(columns={p: f"p_exceed_{_format_p_exceed_label(p)}" for p in fdc_pivot.columns})
            fdc_df = fdc_pivot.round(3)

        except:
            LOG.warning("FDC data not available; falling back to daily data for FDC calculation.")
            # Load daily data from S3 using Dask
            # Convert to a list of integers
            dailyflow_ds = get_daily_ds().sel(river_id=rivids_int)
            # Convert Xarray to Dask DataFrame
            daily_df = dailyflow_ds.to_dataframe().reset_index()

            # Check if daily_df is empty
            if daily_df.empty:
                LOG.warning(f"Skipping processing for {folder.DEM_File} because daily_df is empty.")
                return (None, None)
            
            # creating exceedance percentiles with the daily data
            p_exceedance = [float(v) for v in range(5, 101, 5)]
            p_exceedance.insert(0, 0.0)
            quantiles = [1.0 - (p / 100.0) for p in p_exceedance]
            daily_quantiles = daily_df.groupby('river_id')['Q'].quantile(quantiles).unstack()
            daily_quantiles = daily_quantiles.rename(
                columns={q: f"p_exceed_{_format_p_exceed_label(p)}" for q, p in zip(quantiles, p_exceedance)}
            )
            fdc_df = daily_quantiles.round(3)

            # uniqify the index
            fdc_df = fdc_df[~fdc_df.index.duplicated(keep='first')]

        # Combine the results from retrospective and return periods data
        # final_df = pd.concat([combined_df, rp_pivot_df], axis=1)
        final_df = pd.concat([fdc_df, rp_pivot_df], axis=1)
        final_df['COMID'] = final_df.index

        # Reorder the DataFrame
        columns = [target_column] + [col for col in final_df.columns if col != target_column]
        final_df = final_df[columns]

        # Add a safety factor to one of the columns we could use to run the ARC model
        for col in final_df.columns:
            if col in ['p_exceed_0', 'rp100']:
                final_df[f'{col}_premium'] = round(final_df[col]*10, 3)
        
        LOG.info(final_df)

    elif rivid_field == 'COMID':

        # Fetch return periods (rp2, rp100, etc.)
        final_df = get_nwm_rp(rivids_int, watershed_dict["nwm_api_key"])

        # Add derived flows directly to rp_df without dropping anything
        final_df["rp100_premium"] = (final_df["rp100"] * 10).round(3)

        # Reorder columns so the return period fields come first
        cols = [col for col in final_df.columns if col.startswith("rp")]
        final_df = final_df[cols]

        final_df['COMID'] = final_df.index

        # Reorder the DataFrame
        columns = [target_column] + [col for col in final_df.columns if col != target_column]
        final_df = final_df[columns]

        LOG.info(final_df)

    # Write the final Dask DataFrame to CSV
    final_df.to_csv(folder.DEM_Reanalsyis_FlowFile, index=False)

    # Return the combined DataFrame as a Dask DataFrame
    return rivids_int, StrmShp_filtered_gdf