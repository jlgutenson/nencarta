"""
Download forecast streamflow from GEOGLOWS or the National Water Model, and USGS DEMs where GEOGLOWS forecasts
flooding.

@author: mike, with a TON of help from Riley
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import s3fs
import xarray as xr

from nencarta._constants import GEOGLOWS_FORECAST_PREFIX_URL
from nencarta.core.enumerations import StreamflowSource
from nencarta.download_usgs_dem import download_dem
from nencarta.logger import LOG

NWM_GOOGLE_STORAGE_URL = "https://storage.googleapis.com/national-water-model"

#Hydroviewer=>  https://apps.geoglows.org/apps/geoglows-hydroviewer/      https://beta.apps.geoglows.org/
#Forecast Data=>  http://geoglows-v2-forecasts.s3-website-us-west-2.amazonaws.com/
#Return Period Data=>  http://geoglows-v2-retrospective.s3-website-us-west-2.amazonaws.com/#return-periods/
#Stream Line Data=>  http://geoglows-v2.s3-website-us-west-2.amazonaws.com/#streams/
#Links to all the Data=>  https://data.geoglows.org/available-data


def _normalize_rivids(rivids):
    return pd.Index(pd.Series(rivids).dropna().astype(int).unique())

def _build_target_timestamp(forecastdate, forecasthour=None):
    timestamp_text = forecastdate
    timestamp_format = "%Y%m%d"

    if forecasthour not in (None, ""):
        timestamp_text = f"{forecastdate}{int(forecasthour):02d}"
        timestamp_format = "%Y%m%d%H"

    return pd.Timestamp(datetime.strptime(timestamp_text, timestamp_format))

def _get_dataset_time_range(ds):
    time_values = ds["time"].values
    return pd.Timestamp(time_values[0]), pd.Timestamp(time_values[-1])

def _finalize_single_value_forecast(flow_df, id_column, value_column, column_order):
    if flow_df.empty:
        raise ValueError("The retrospective selection returned no rows.")

    df_max = flow_df[[id_column, value_column]].dropna(subset=[value_column]).copy()
    if df_max.empty:
        raise ValueError("The retrospective selection returned only missing streamflow values.")

    df_max = df_max.rename(columns={id_column: "rivid"})
    df_max["rivid"] = df_max["rivid"].astype(int)
    df_max["median"] = df_max[value_column].astype(float).round(2)
    df_max["min"] = df_max["median"]
    df_max["max"] = df_max["median"]

    return df_max[column_order]

def _read_geoglows_retrospective_flow(forecastdate, rivids):
    target_time = _build_target_timestamp(forecastdate)
    normalized_rivids = _normalize_rivids(rivids)
    retro_ds = xr.open_zarr(
        "s3://geoglows-v2/retrospective/daily.zarr",
        storage_options={"anon": True},
    )

    try:
        flow_df = (
            retro_ds[["Q"]]
            .sel(river_id=normalized_rivids, time=target_time)
            .to_dataframe()
            .reset_index()
        )
    except KeyError as exc:
        start_time, end_time = _get_dataset_time_range(retro_ds)
        raise ValueError(
            f"GEOGLOWS retrospective data are unavailable for {target_time:%Y-%m-%d}. "
            f"Available dates span {start_time:%Y-%m-%d} through {end_time:%Y-%m-%d}."
        ) from exc

    LOG.info(
        "Using GEOGLOWS retrospective daily streamflow fallback for %s",
        target_time.strftime("%Y-%m-%d"),
    )
    return _finalize_single_value_forecast(
        flow_df,
        id_column="river_id",
        value_column="Q",
        column_order=["rivid", "median", "min", "max"],
    )

def _read_nwm_retrospective_flow(forecastdate, forecasthour, rivids):
    if forecasthour in (None, ""):
        raise ValueError("forecasthour is required for NWM retrospective fallback.")

    target_time = _build_target_timestamp(forecastdate, forecasthour)
    normalized_rivids = _normalize_rivids(rivids)
    retro_ds = xr.open_zarr(
        "s3://noaa-nwm-retrospective-3-0-pds/CONUS/zarr/chrtout.zarr",
        storage_options={"anon": True},
        consolidated=True,
    )

    try:
        flow_df = (
            retro_ds[["streamflow"]]
            .sel(feature_id=normalized_rivids, time=target_time)
            .to_dataframe()
            .reset_index()
        )
    except KeyError as exc:
        start_time, end_time = _get_dataset_time_range(retro_ds)
        raise ValueError(
            f"NWM retrospective data are unavailable for {target_time:%Y-%m-%d %H:%M:%S}. "
            f"Available times span {start_time:%Y-%m-%d %H:%M:%S} through "
            f"{end_time:%Y-%m-%d %H:%M:%S}."
        ) from exc

    LOG.info(
        "Using NWM retrospective hourly streamflow fallback for %s",
        target_time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return _finalize_single_value_forecast(
        flow_df,
        id_column="feature_id",
        value_column="streamflow",
        column_order=["rivid", "min", "max", "median"],
    )

def _nwm_google_forecast_specs(streamflow_source):
    if streamflow_source == StreamflowSource.NWM_SHORT_RANGE:
        return [("short_range", "short_range.channel_rt", range(1, 18))]

    if streamflow_source == StreamflowSource.NWM_MEDIUM_RANGE:
        specs = [("medium_range", "medium_range.channel_rt", range(3, 240, 3))]
        specs.extend(
            (f"medium_range_mem{i}", f"medium_range.channel_rt_{i}", range(3, 240, 3))
            for i in range(1, 6)
        )
        return specs

    if streamflow_source == StreamflowSource.NWM_LONG_RANGE:
        specs = [("long_range", "long_range.channel_rt", range(6, 720, 6))]
        specs.extend(
            (f"long_range_mem{i}", f"long_range.channel_rt_{i}", range(6, 720, 6))
            for i in range(1, 4)
        )
        return specs

    raise ValueError(f"streamflow_source {streamflow_source} is not an NWM forecast source.")

def _read_nwm_google_netcdf_streamflow(url, normalized_rivids):
    response = requests.get(url, timeout=60)
    if response.status_code == 404:
        return pd.DataFrame(columns=["rivid", "streamflow"])
    response.raise_for_status()

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as temp_file:
            temp_file.write(response.content)
            temp_path = temp_file.name

        with xr.open_dataset(temp_path) as ds:
            if "feature_id" not in ds or "streamflow" not in ds:
                raise ValueError(f"NWM forecast file is missing feature_id or streamflow: {url}")

            available_rivids = pd.Index(ds["feature_id"].values).astype(int).intersection(normalized_rivids)
            if available_rivids.empty:
                return pd.DataFrame(columns=["rivid", "streamflow"])

            try:
                flow_df = (
                    ds[["streamflow"]]
                    .sel(feature_id=available_rivids)
                    .to_dataframe()
                    .reset_index()
                )
            except KeyError:
                flow_df = ds[["streamflow"]].to_dataframe().reset_index()
                flow_df = flow_df[flow_df["feature_id"].astype(int).isin(available_rivids)]

        flow_df = flow_df.rename(columns={"feature_id": "rivid"})
        flow_df["rivid"] = flow_df["rivid"].astype(int)
        return flow_df[["rivid", "streamflow"]].dropna(subset=["streamflow"])
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                LOG.warning("Could not remove temporary NWM forecast file: %s", temp_path)

def _read_nwm_google_storage_forecast(forecastdate, forecasthour, rivids, streamflow_source):
    if forecasthour in (None, ""):
        raise ValueError("forecasthour is required for NWM Google Storage forecast fallback.")

    forecast_hour = int(forecasthour)
    normalized_rivids = _normalize_rivids(rivids)
    member_df_list = []

    LOG.info(
        "Attempting NWM public Google Storage forecast fallback for forecastdate=%s forecasthour=%02d",
        forecastdate,
        forecast_hour,
    )

    for forecast_folder, product_name, lead_hours in _nwm_google_forecast_specs(streamflow_source):
        forecast_frames = []
        for lead_hour in lead_hours:
            url = (
                f"{NWM_GOOGLE_STORAGE_URL}/nwm.{forecastdate}/{forecast_folder}/"
                f"nwm.t{forecast_hour:02d}z.{product_name}.f{lead_hour:03d}.conus.nc"
            )
            flow_df = _read_nwm_google_netcdf_streamflow(url, normalized_rivids)
            if not flow_df.empty:
                forecast_frames.append(flow_df)

        if not forecast_frames:
            LOG.warning(
                "No NWM Google Storage forecast files were available for folder=%s product=%s",
                forecast_folder,
                product_name,
            )
            continue
        if forecast_frames:
            LOG.info(
                "NWM Google Storage forecast files were available for folder=%s product=%s",
                forecast_folder,
                product_name,
            )

        member_df = pd.concat(forecast_frames).groupby("rivid", sort=False).max()
        member_df_list.append(member_df)

    if not member_df_list:
        raise ValueError(
            f"No NWM Google Storage forecast streamflow rows were found for {forecastdate} "
            f"hour {forecast_hour:02d} and {len(normalized_rivids)} rivids."
        )

    combined_df = pd.concat(member_df_list)
    return combined_df.groupby("rivid").agg(
        min=("streamflow", "min"),
        max=("streamflow", "max"),
        median=("streamflow", "median")
    ).reset_index()

def Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, CSV_File_Name, streamflow_source: StreamflowSource, nwm_api_key=None):
    """
    Write each river's forecast peak flow to ``CSV_File_Name`` and return it: the median, minimum and maximum,
    across the ensemble members, of each member's highest flow. NWM forecasts come from the NWM API, or NOAA's
    public Google Storage bucket if that fails. If no forecast can be read, the retrospective flow on that date is
    used instead.
    """
    try:
        if streamflow_source == StreamflowSource.GEOGLOWS_V2:
            LOG.info('Pulling ' + str(len(rivids)) + ' river ids from GeoGLOWS Forecast Bucket')
            # Get the max value for each ensemble and rivid, then get the median, min, and max for each rivid
            df_max = (
                xr.open_zarr(f'{GEOGLOWS_FORECAST_PREFIX_URL}{forecastdate}00.zarr', storage_options={"anon": True})
                .sel(rivid=rivids)
                .to_dataframe()
                .round(2)
                .reset_index()
                .groupby(['ensemble', 'rivid'], sort=False)
                .max()
                .groupby('rivid', sort=False)
                .agg(median=('Qout', 'median'), min=('Qout', 'min'), max=('Qout', 'max'))
                .reset_index()
            )
            
            LOG.info(f'Writing output file: {CSV_File_Name}')
        elif streamflow_source.is_nwm():
            rp_url = 'https://nwm-api.ciroh.org/forecast'


            if streamflow_source == StreamflowSource.NWM_SHORT_RANGE:
                forecast_type = 'short_range'
                number_of_ensembles = 0
            elif streamflow_source == StreamflowSource.NWM_MEDIUM_RANGE:
                forecast_type = 'medium_range'
                number_of_ensembles = 5
            elif streamflow_source == StreamflowSource.NWM_LONG_RANGE:
                forecast_type = 'long_range'
                number_of_ensembles = 3


            # Reformat the forecastdate which is currently YYYYMMDD to "2023-11-25 06:00:00 UTC"
            forecastdate_formatted = f"{forecastdate[:4]}-{forecastdate[4:6]}-{forecastdate[6:]} {forecasthour}:00:00 UTC"
            LOG.info(f"Requesting NWM forecast data for {forecastdate_formatted} for {len(rivids)} rivids")

            # loop through the ensemble members
            nwm_ensemble_df_list = []
            if not nwm_api_key:
                raise ValueError("nwm_api_key is required for NWM forecast requests.")

            try:
                for i in range(number_of_ensembles + 1):

                    header = {'x-api-key': nwm_api_key}
                    params = {'forecast_type': forecast_type,
                            'reference_time': forecastdate_formatted,
                            'comids': ','.join(map(str, rivids)),
                            'output_format': 'csv',
                            'ensemble': i  # ensemble member
                            }

                    response = requests.get(rp_url, params=params, headers=header, timeout=60)







                    if response.status_code != 200:
                        raise requests.exceptions.HTTPError(response.text)


                    forecast_df = pd.read_csv(io.StringIO(response.text))
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
            except requests.exceptions.HTTPError as nwm_api_exc:
                LOG.warning(
                    "NWM API forecast request failed; attempting public Google Storage fallback. Error: %s",
                    nwm_api_exc,
                )
                df_max = _read_nwm_google_storage_forecast(
                    forecastdate,
                    forecasthour,
                    rivids,
                    streamflow_source,
                )
        else:
            raise ValueError(f"streamflow_source {streamflow_source} not recognized.")

        if df_max.empty:
            raise ValueError("The primary forecast request returned no streamflow rows.")

    except Exception as forecast_exc:  # noqa: BLE001
        LOG.warning(
            "Primary %s streamflow access failed for forecastdate=%s forecasthour=%s. "
            "Attempting retrospective fallback. Error: %s",
            streamflow_source,
            forecastdate,
            forecasthour,
            forecast_exc,
        )

        try:
            if streamflow_source == StreamflowSource.GEOGLOWS_V2:
                df_max = _read_geoglows_retrospective_flow(forecastdate, rivids)
            elif streamflow_source.is_nwm():
                df_max = _read_nwm_retrospective_flow(forecastdate, forecasthour, rivids)
            else:
                raise ValueError(f"streamflow_source {streamflow_source} not recognized.")
        except Exception as retrospective_exc:
            raise RuntimeError(
                f"Unable to access {streamflow_source} streamflow for forecastdate={forecastdate} "
                f"and forecasthour={forecasthour}. Forecast access failed with: {forecast_exc}. "
                f"Retrospective fallback failed with: {retrospective_exc}"
            ) from retrospective_exc

    #Write the output to a csv file
    df_max.to_csv(CSV_File_Name, index=False)
    LOG.info('\nExample of the output data....')
    LOG.info(df_max)

    return df_max

def Download_USGS_DEM_Data_Using_WarningFlag_Data(vpu, DEM_save_dir, forensic_forecast_date):
    """
    Download the USGS 1/3 arc-second DEM tiles, in CONUS, around the rivers of GEOGLOWS VPU ``vpu`` that are
    forecast to flood (exceed a return period) on ``forensic_forecast_date``, or on the newest forecast of the last
    week when that is None. Returns the DEM paths. Tiles that don't exist are listed in ``bad_urls.csv`` in
    ``DEM_save_dir`` and skipped from then on.
    """
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
    s3 = s3fs.S3FileSystem(anon=True, client_kwargs={'region_name': ODP_S3_BUCKET_REGION})

    # Start with today's date in UTC
    current_date = datetime.now(tz=timezone.utc)

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
            
        except ValueError:
            LOG.info("Something is up with the GEOGLOWS warning flag data while trying to use the forensic forecast date, please check your date or please try again later and/or notify GEOGLOWS...")
            sys.exit()
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
            LOG.info("Something is up with the GEOGLOWS warning flag data, please try again later and/or notify GEOGLOWS...")
            sys.exit()

    # uniqueify a list of "comid" values from the warning flag dataframe
    rivids = warning_flag_df['comid'].unique()

    # get the latitude and longitude for the comids in the warning flag dataframe
    parquet_path = 's3://geoglows-v2/tables/package-metadata-table.parquet'
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
        DEM_save_path, _, bad_url_list = download_dem(lat, lon, bad_url_list, DEM_save_dir)
        if DEM_save_path is not None:
            DEMs_with_flooding_forecast.append(DEM_save_path)

    # uniqueify the DEM list
    DEMs_with_flooding_forecast = list(dict.fromkeys(DEMs_with_flooding_forecast))

    # write the bad urls to a csv file
    if len(bad_url_list) > 0:
        bad_url_df = pd.DataFrame(bad_url_list, columns=['bad_url'])
        bad_url_df.to_csv(bad_urls_csv_path, index=False)
        LOG.info(f"Bad URLs written to {bad_urls_csv_path}")

    return DEMs_with_flooding_forecast
    

def Get_Date_For_Forecast(day_back, hour_back, streamflow_source: StreamflowSource):
    """
    Return the date (YYYYMMDD) and hour (HH, or None for GEOGLOWS, whose forecasts are daily) of the forecast
    issued ``day_back`` days and ``hour_back`` hours before now (UTC), snapped to a cycle the source publishes.
    """

    # get the date and time in UTC
    today = datetime.now(timezone.utc)
    LOG.info('Current date:  ' + str(today))

    new_date = today - timedelta(days=day_back, hours=hour_back)
    LOG.info('Forecast date: ' + str(new_date))
    forecastdate = new_date.strftime("%Y%m%d")
    LOG.info('Forecast date: ' + str(forecastdate))
    forecasthour = new_date.strftime("%H")

    # set forecast hour based upon the streamflow source
    # GEOGLOWS: daily — hour doesn't matter
    if streamflow_source == StreamflowSource.GEOGLOWS_V2:
        forecasthour = None  # explicitly signal "daily"

    # NWM short range: hour can be 2 hours behind the current forecast hour
    elif streamflow_source == StreamflowSource.NWM_SHORT_RANGE:
        h = int(forecasthour) - 2
        if h < 0:
            # roll back a day and wrap hour into prior day
            forecastdate = (new_date - timedelta(days=1)).strftime("%Y%m%d")
            h += 24
        forecasthour = f"{h:02d}"

    # NWM medium range: choose the closest cycle among 00, 06, 12, 18 that does NOT exceed the current hour
    elif streamflow_source == StreamflowSource.NWM_MEDIUM_RANGE:
        h_now = int(forecasthour)
        # allowed cycle hours
        cycles = [0, 6, 12, 18]
        # pick the greatest cycle <= current hour; if current hour < 0 (impossible), default to 00
        cycle = max([c for c in cycles if c <= h_now], default=0)
        forecasthour = f"{cycle:02d}"

    # NWM long range: the forecast hour must be "00"
    elif streamflow_source == StreamflowSource.NWM_LONG_RANGE:
        forecasthour = "00"

    else:
        # Unknown source — leave as derived from base
        pass    
        
    return forecastdate, forecasthour
