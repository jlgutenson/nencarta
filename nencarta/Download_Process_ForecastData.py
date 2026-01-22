# -*- coding: utf-8 -*-
"""
Created on Wed Apr 10 16:47:37 2024

@author: mike, with a TON of help from Riley
"""

# local imports
from . import Download_USGS_DEM
from .logger import LOG

import io
import pandas as pd
import geopandas as gpd  #pip3 install geopandas
import requests
import s3fs
import xarray as xr

# built-in imports
from datetime import datetime, timedelta, datetime, timezone
import os
import sys


#Hydroviewer=>  https://apps.geoglows.org/apps/geoglows-hydroviewer/      https://beta.apps.geoglows.org/
#Forecast Data=>  http://geoglows-v2-forecasts.s3-website-us-west-2.amazonaws.com/
#Return Period Data=>  http://geoglows-v2-retrospective.s3-website-us-west-2.amazonaws.com/#return-periods/
#Stream Line Data=>  http://geoglows-v2.s3-website-us-west-2.amazonaws.com/#streams/
#Links to all the Data=>  https://data.geoglows.org/available-data


def Get_RIVIDs_From_Shapefile(StrmShpFile):
    df = gpd.read_file(StrmShpFile)

    #df = df[df['TerminalLink'] == TermLinkNumber]   #This gathers all of the streams that drain to this point.
    
    rivids = df['LINKNO'].astype(int).values
    return rivids

def Get_RIVIDs_From_Terminal_Link(parquet_file_from_geoglows, TermLinkNumber):
    
    df = pd.read_parquet(parquet_file_from_geoglows)   #http://geoglows-v2.s3-website-us-west-2.amazonaws.com/#tables/

    df = df[df['TerminalLink'] == TermLinkNumber]   #This gathers all of the streams that drain to this point.
    
    rivids = df['LINKNO'].values
    return rivids

def Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, CSV_File_Name, streamflow_source, nwm_api_key=None):

    if streamflow_source == 'GEOGLOWS':
        ODP_FORECAST_S3_BUCKET_URI = 's3://geoglows-v2-forecasts'
        
        ODP_S3_BUCKET_REGION = 'us-west-2'
        
        s3 = s3fs.S3FileSystem(anon=True, client_kwargs=dict(region_name=ODP_S3_BUCKET_REGION))
        
        s3store = s3fs.S3Map(root=f'{ODP_FORECAST_S3_BUCKET_URI}/{forecastdate}00.zarr', s3=s3, check=False)
        
        
        LOG.info('Pulling ' + str(len(rivids)) + ' river ids from GeoGLOWS Forecast Bucket')
        df = xr.open_zarr(s3store).sel(rivid=rivids).to_dataframe().round(2).reset_index()
        
        #This was just for testing to see all the values for a single RIVID
        #CSV_File_Name = 'testcase.csv'
        #df.to_csv(CSV_File_Name, index=False)
        #LOG.info(df.columns)
        
        #Create a new column called riv_ens, which is just the rivid with the ensemble number tagged on the end.
        LOG.info('Calculating the peak flow for each ensemble member of each rivid')
        new_col_num = len(df.columns)
        riv_ens = df.rivid.values.astype(int)
        df.insert(new_col_num,'riv_ens',riv_ens)
        df['riv_ens'] = df['riv_ens'].apply(lambda x: x*100) + df['ensemble']
        
        #Find the max value for each ensemble for each rivid
        maxflows = df.reset_index().groupby('riv_ens').max()['Qout']
        
        #Create lists of the riv_ens and the max flow for each rivid and ensemble.  Should be an easier way to do this, but I'm not that good with pd
        riv_ens_list = maxflows.index.tolist()
        maxflows = list(maxflows)
        
        #Create a dataframe that has the riv_ens and the max flow rate (Qmax).  Qmax is for each ensemble of each rivid.
        LOG.info('Evaluting the min/med/max peak flows of the ensemble-members for each rivid')
        df_max = pd.DataFrame(list(zip(riv_ens_list, maxflows)), columns=['riv_ens', 'Qmax'])
        #LOG.info(df_max.columns)
        
        #Create a new column for the rivid.  Simply divide the riv_ens by 100 to get the rivid.
        new_col_num = len(df_max.columns)
        riv_ens = df_max.riv_ens.values.astype(int)
        df_max.insert(new_col_num,'rivid',riv_ens)
        df_max['rivid'] = df_max['riv_ens'].apply(lambda x: int(x/100))
        
        #We now have the peak flow for each ensemble for each rivid.
        #Now we want to calculate the max, min, and median of the peak flow amounts
        max_series = df_max.reset_index().groupby('rivid').max()['Qmax']
        min_series = df_max.reset_index().groupby('rivid').min()['Qmax']
        median_series = df_max.reset_index().groupby('rivid').median()['Qmax'].to_frame()
        
        #Collect all the information and print to a csv.
        LOG.info('Writing output file: ' + CSV_File_Name)
        df_max = median_series.merge(min_series, left_index=True, right_index=True).merge(max_series, left_index=True, right_index=True)
        df_max.columns = ['median', 'min', 'max']
        df_max = df_max.reset_index()
    
    elif streamflow_source.startswith('NWM'):
        rp_url = 'https://nwm-api.ciroh.org/forecast'


        if streamflow_source == 'NWM_short_range':
            forecast_type = 'short_range'
            number_of_ensembles = 0
        elif streamflow_source == 'NWM_medium_range':
            forecast_type = 'medium_range'
            number_of_ensembles = 5
        elif streamflow_source == 'NWM_long_range':
            forecast_type = 'long_range'
            number_of_ensembles = 3


        # Reformat the forecastdate which is currently YYYYMMDD to "2023-11-25 06:00:00 UTC"
        forecastdate_formatted = f"{forecastdate[:4]}-{forecastdate[4:6]}-{forecastdate[6:]} {forecasthour}:00:00 UTC"
        LOG.info(f"Requesting NWM forecast data for {forecastdate_formatted} for {len(rivids)} rivids")

        # loop through the five medium-range ensemble members
        nwm_ensemble_df_list = []
        if not nwm_api_key:
            raise ValueError("nwm_api_key is required for NWM forecast requests.")

        for i in range(0, number_of_ensembles + 1):

            header = {'x-api-key': nwm_api_key}
            params = {'forecast_type': forecast_type,
                    'reference_time': forecastdate_formatted,
                    'comids': ','.join(map(str, rivids)),
                    'output_format': 'csv',
                    'ensemble': i  # ensemble member
                    }

            response = requests.get(rp_url, params=params, headers=header, timeout=60)

            if response.status_code == 200:
                forecast_df = pd.read_csv(io.StringIO(response.text))
            else:
                raise requests.exceptions.HTTPError(response.text)
            forecast_df = forecast_df.set_index("feature_id")
            forecast_df.index.name = "rivid"
            # group by rivid and find the max value for each rivid
            forecast_df = forecast_df.groupby("rivid").max()
            # append to the ensemble list
            nwm_ensemble_df_list.append(forecast_df)
        
        # Concatenate all dataframes in the list
        combined_df = pd.concat(nwm_ensemble_df_list)

        df_max = combined_df.groupby("rivid").agg(
            min=("streamflow", "min"),
            max=("streamflow", "max"),
            median=("streamflow", "median")
        ).reset_index()

    #Write the output to a csv file
    df_max.to_csv(CSV_File_Name, index=False)
    LOG.info('Example of the output data....')
    LOG.info(df_max)

    return df_max

def Download_USGS_DEM_Data_Using_WarningFlag_Data(vpu, DEM_save_dir, forensic_forecast_date):
    # Configuration
    ODP_FORECAST_S3_BUCKET_URI = 's3://geoglows-v2-forecast-products'
    ODP_S3_BUCKET_REGION = 'us-west-2'

    # Check if the directory contains a csv file called 'bad_urls.csv'
    bad_urls_csv_path = os.path.join(DEM_save_dir, 'bad_urls.csv')
    if os.path.exists(bad_urls_csv_path):
        # Read the CSV file into a DataFrame
        bad_url_df = pd.read_csv(bad_urls_csv_path)
        # Convert the 'bad_url' column to a list
        bad_url_list = bad_url_df['bad_url'].tolist()
    # if doesn't exist, initialize an empty list
    else:
        bad_url_list = []

    # Initialize S3 file system (anonymous access)
    s3 = s3fs.S3FileSystem(anon=True, client_kwargs=dict(region_name=ODP_S3_BUCKET_REGION))

    # Start with today's date in UTC
    current_date = datetime.utcnow()

    # if forensic_forecast_date is provided, use it to set the current date
    if forensic_forecast_date is not None:
        try:
            # Parse the forensic forecast date
            forecastdate = forensic_forecast_date
            LOG.info(f"Using forensic forecast date: {forecastdate}")
            warning_flag_df = None
            
            # Construct parquet path
            parquet_path = f'{ODP_FORECAST_S3_BUCKET_URI}/map-tables/{forecastdate}00/mapstyletable_{vpu}_{forecastdate}00.parquet'

            # Try to read the parquet file
            warning_flag_df = pd.read_parquet(parquet_path, engine='pyarrow', filesystem=s3)
            warning_flag_df = warning_flag_df[warning_flag_df['ret_per']> 0]
            LOG.info(f"Successfully loaded forecast warning flags for date: {forecastdate}")
            
        except ValueError as e:
            LOG.error("Something is up with the GEOGLOWS warning flag data while trying to use the forensic forecast date, please check your date or please try again later and/or notify GEOGLOWS...")
            raise e
    else:
        # don't try more than seven days in the past
        days_past = 0
        while True and days_past <= 7:
            # today date to be used to find the newest warning point forecast
            forecastdate = current_date.strftime('%Y%m%d')

            # default to warning_flag_df = None
            warning_flag_df = None
            
            # Construct parquet path
            parquet_path = f'{ODP_FORECAST_S3_BUCKET_URI}/map-tables/{forecastdate}00/mapstyletable_{vpu}_{forecastdate}00.parquet'
            
            try:
                # Try to read the parquet file
                warning_flag_df = pd.read_parquet(parquet_path, engine='pyarrow', filesystem=s3)
                warning_flag_df = warning_flag_df[warning_flag_df['ret_per']> 0]
                LOG.info(f"Successfully loaded forecast warning flags for date: {forecastdate}")
                break  # Exit loop on success
            except FileNotFoundError:
                # Go back one day if file not found
                LOG.info(f"Warning flag parquet file not found for {forecastdate}, trying previous day...")
                current_date -= timedelta(days=1)
                days_past = days_past + 1
                warning_flag_df = None
        
        if days_past == 7 and warning_flag_df is None:
            LOG.error("Something is up with the GEOGLOWS warning flag data, please try again later and/or notify GEOGLOWS...")
            raise FileNotFoundError("Could not find a valid warning flag parquet file in the past 7 days.")

    # uniqueify a list of "comid" values from the warning flag dataframe
    rivids = warning_flag_df['comid'].unique()

    # get the latitude and longitude for the comids in the warning flag dataframe
    parquet_path = f's3://geoglows-v2/tables/package-metadata-table.parquet'
    lat_lon_df = pd.read_parquet(parquet_path, engine='pyarrow', filesystem=s3)

    # filter the lat_lon_df using rivids from the warning flags df
    lat_lon_df = lat_lon_df[lat_lon_df['LINKNO'].isin(rivids)]

    # convert the 'lat' and 'lon' columns to integers
    lat_lon_df['lat'] = lat_lon_df['lat'].astype(int)
    lat_lon_df['lon'] = lat_lon_df['lon'].astype(int)

    # filter lat_long_df to only include unique lat/lon value combinations
    lat_lon_df = lat_lon_df.drop_duplicates(subset=['lat', 'lon'])

    # CONUS extemes for all the lat/lon values
    lat_high = 49
    lat_low = 24
    lon_high = -66
    lon_low = -125

    # check if the lat/lon values are within the CONUS extremes and filter out any that are not
    lat_lon_df = lat_lon_df[(lat_lon_df['lat'] > lat_low) & (lat_lon_df['lat'] < lat_high) &
                            (lat_lon_df['lon'] > lon_low) & (lat_lon_df['lon'] < lon_high)]
    

    # loop through the lat_lon_df for each 'lat' and 'lon' value and pass those to the DEM download process
    DEMs_with_flooding_forecast = []
    for index, row in lat_lon_df.iterrows():
        lat = row['lat']
        lon = row['lon']
        DEM_save_path, DEM_Filename, bad_url_list = Download_USGS_DEM.download_dem(lat, lon, bad_url_list, DEM_save_dir)
        if DEM_save_path is not None:
            DEMs_with_flooding_forecast.append(DEM_Filename)

    # uniqueify the DEM list
    DEMs_with_flooding_forecast = list(set(DEMs_with_flooding_forecast))

    # write the bad urls to a csv file
    if len(bad_url_list) > 0:
        bad_url_df = pd.DataFrame(bad_url_list, columns=['bad_url'])
        bad_url_df.to_csv(bad_urls_csv_path, index=False)
        LOG.info(f"Bad URLs written to {bad_urls_csv_path}")

    return DEMs_with_flooding_forecast
    

def Get_RIVID_Values(Riv_Method, parquet_file_from_geoglows, TermLinkNumber, StrmShpFile):
    
    if Riv_Method == 'TerminalLink':
        rivids = Get_RIVIDs_From_Terminal_Link(parquet_file_from_geoglows, TermLinkNumber)
    
    if Riv_Method == 'Shapefile':
        rivids = Get_RIVIDs_From_Shapefile(StrmShpFile)
    
    #rivids = [280706358, 280759351]
    return rivids

def Get_Date_For_Forecast(day_back, hour_back, streamflow_source):

    # get the date and time in UTC
    today = datetime.now(timezone.utc)
    # today = date.today()
    LOG.info('Current date:  ' + str(today))
    
    new_date = today - timedelta(days=day_back, hours=hour_back)
    LOG.info('Forecast date: ' + str(new_date))
    forecastdate = new_date.strftime("%Y%m%d")
    LOG.info('Forecast date: ' + str(forecastdate))
    forecasthour = new_date.strftime("%H")

    # set forecast hour based upon the streamflow source
    # GEOGLOWS: daily — hour doesn't matter
    if streamflow_source == "GEOGLOWS":
        forecasthour = None  # explicitly signal "daily"

    # NWM short range: hour can be 2 hours behind the current forecast hour
    elif streamflow_source == "NWM_short_range":
        h = int(forecasthour) - 2
        if h < 0:
            # roll back a day and wrap hour into prior day
            base = base - timedelta(days=1)
            forecastdate = base.strftime("%Y%m%d")
            h += 24
        forecasthour = f"{h:02d}"

    # NWM medium range: choose the closest cycle among 00, 06, 12, 18 that does NOT exceed the current hour
    elif streamflow_source == "NWM_medium_range":
        h_now = int(forecasthour)
        # allowed cycle hours
        cycles = [0, 6, 12, 18]
        # pick the greatest cycle <= current hour; if current hour < 0 (impossible), default to 00
        cycle = max([c for c in cycles if c <= h_now], default=0)
        forecasthour = f"{cycle:02d}"

    # NWM long range: the forecast hour must be "00"
    elif streamflow_source == "NWM_long_range":
        forecasthour = "00"

    else:
        # Unknown source — leave as derived from base
        pass    
        
    return forecastdate, forecasthour


if __name__ == "__main__":
    #MAIN INPUTS
    Riv_Method = 'Shapefile'  #Options are 'TerminalLink' or 'Shapefile'
    parquet_file_from_geoglows = 'v2-model-table.parquet'     #http://geoglows-v2.s3-website-us-west-2.amazonaws.com/#tables/
    TermLinkNumber = 280274809   #280274809 is the Volga River before it dumps into the Caspian Sea
    StrmShpFile = r"C:\Users\jlgut\OneDrive\Desktop\AutomatedRatingCurve_TestCase\Yellowstone_TestCase\StrmShp\Yellowstone_Streams_GeoGLoWS_4269_clipped_to_basin.shp"

    
    #Get list of rivids that you want forecast values for
    rivids = Get_RIVID_Values(Riv_Method, parquet_file_from_geoglows, TermLinkNumber, StrmShpFile)
    
    streamflow_source = "GEOGLOWS"
    forecastdate, forecasthour = Get_Date_For_Forecast(1, 0, streamflow_source)
    
    CSV_File_Name = 'PeakFlow_GeoGLOWS_forecast.csv'
    LOG.info('Forecast data will written to ' + CSV_File_Name)
    
    Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, CSV_File_Name, streamflow_source, nwm_api_key=None)
