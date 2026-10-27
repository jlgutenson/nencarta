import io
import os
from pathlib import Path
from functools import cache

import requests
import numpy as np
import pandas as pd
import xarray as xr

from nencarta.logger import LOG
from nencarta.core.vector import Vector
from nencarta.workspace import Workspace
from nencarta.core.enumerations import StreamflowSource
from nencarta.exceptions import NoStreamsFoundException
from nencarta._constants import GEOGLOWS_RETURN_PERIODS_URL, GEOGLOWS_FDC_URL, GEOGLOWS_DAILY_URL, NWM_RP_URL

@cache
def get_rp_ds():
    """ This is faster for multiprocessing contexts since the dataset is only loaded once per process."""
    return xr.open_zarr(GEOGLOWS_RETURN_PERIODS_URL, storage_options={'anon': True})

@cache
def get_fdc_ds():
    return xr.open_zarr(GEOGLOWS_FDC_URL, storage_options={'anon': True})


@cache
def get_daily_ds():
    return xr.open_zarr(GEOGLOWS_DAILY_URL, storage_options={'anon': True})

def _get_geoglows_rp(river_ids: list[int]) -> pd.DataFrame:
    rp_ds = get_rp_ds().sel(river_id=river_ids)[['gumbel', 'gumbel_hourly', 'gumbel_daily']]
        
    # Convert Xarray to Dask DataFrame and pivot
    rp_df = rp_ds.to_dataframe().reset_index()

    # find the maximum between the gumbel, gumbel_hourly, and gumbel_daily return periods and label this new column 'return_period_flow'
    rp_df['return_period_flow'] = rp_df[['gumbel', 'gumbel_hourly', 'gumbel_daily']].max(axis=1).round(3)

    # drop any rows where 'return_period_flow' is NaN, infinite, or zero
    rp_df = rp_df.dropna(subset=['return_period_flow'])
    rp_df = rp_df[~rp_df['return_period_flow'].isin([float('inf'), 0])]

    # keep just the column 'return_period_flow'
    rp_df = rp_df[['river_id', 'return_period', 'return_period_flow']]

    # Convert 'return_period' to category dtype
    rp_df['return_period'] = rp_df['return_period'].astype('category')
    
    # Pivot the table
    rp_df = rp_df.pivot_table(index='river_id', columns='return_period', values='return_period_flow', aggfunc='mean', observed=False)

    # Rename columns to indicate return periods
    rp_df = rp_df.rename(columns={col: f'rp{int(col)}' for col in rp_df.columns})

    if rp_df.empty:
        # Create a dataframe with 0s for all return periods if no data is available
        rp_df = pd.DataFrame(0, index=river_ids, columns=[f'rp{int(col)}' for col in [2, 5, 10, 25, 50, 100]])
        rp_df.index.name = 'river_id'
    

    p_exceedances = np.arange(0, 106, 5, dtype=float)
    p_exceedances[-1] = 1
    try:
        fdc_ds = get_fdc_ds().sel(p_exceed=p_exceedances, river_id=river_ids)

        # Convert Xarray to Dask DataFrame
        fdc_df = fdc_ds.to_dataframe().reset_index()

        fdc_df = fdc_df.pivot_table(
            index='river_id',
            columns='p_exceed',
            values='hourly_annual',
            aggfunc='mean'
        )
        fdc_df = fdc_df.rename(columns={p: f"p_exceed_{p}" for p in fdc_df.columns})
    except:
        LOG.warning("FDC data not available; falling back to daily data for FDC calculation.")
        # Load daily data from S3 using Dask
        # Convert to a list of integers
        dailyflow_ds = get_daily_ds().sel(river_id=river_ids)
        # Convert Xarray to Dask DataFrame
        daily_df = dailyflow_ds.to_dataframe().reset_index()

        # creating exceedance percentiles with the daily data
        quantiles = [1.0 - (p / 100.0) for p in p_exceedances]
        fdc_df = daily_df.groupby('river_id')['Q'].quantile(quantiles).unstack()
        fdc_df = fdc_df.rename(
            columns={q: f"p_exceed_{p}" for q, p in zip(quantiles, p_exceedances)}
        )

        # uniqify the index
        fdc_df = fdc_df[~fdc_df.index.duplicated(keep='first')]

    final_df = pd.concat([fdc_df, rp_df], axis=1)
    final_df['COMID'] = final_df.index

    # Reorder the DataFrame
    columns = ['COMID'] + [col for col in final_df.columns if col != 'COMID']
    final_df = final_df[columns]

    for col in ['p_exceed_0', 'rp100']:
        # I think this is a better way of buffering the maximum flow
        # Multiping by 1.5 seems to be a reasonable esimate of the maximum high flow, while adding 50 helps small rivers with tiny
        # return period 100 flows (close to 0)
        # Going too big means the VDT has bigger gaps to fill, which can lead to worse performance and less accurate rating curves
        final_df[f'{col}_premium'] = (final_df[col] * 1.5) + 50
        # final_df[f'{col}_premium'] = final_df[col] * 10

    final_df = final_df.round(3)
    return final_df

NWM_RP_BATCH_SIZE = 100


def _request_nwm_rp_batch(comids: list[int], header: dict[str, str]) -> pd.DataFrame:
    """Fetch a bounded batch, splitting further if a server rejects its size."""
    params = {'comids': ','.join(map(str, comids)),
              'output_format': 'csv',
              'order_by_comid': False,}
    response = requests.get(NWM_RP_URL, params=params, headers=header, timeout=60)

    if response.status_code in (413, 414) and len(comids) > 1:
        midpoint = len(comids) // 2
        return pd.concat([
            _request_nwm_rp_batch(comids[:midpoint], header),
            _request_nwm_rp_batch(comids[midpoint:], header),
        ], ignore_index=True)

    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"NWM return period request failed (HTTP {response.status_code}, "
            f"{len(comids)} COMIDs).",
            response=response,
            request=response.request,
        )
    return pd.read_csv(io.StringIO(response.text))


def _get_nwm_rp(comids: list[int], nwm_api_key: str):
    if not nwm_api_key:
        raise ValueError("nwm_api_key is required for NWM return period requests.")

    # Keep the query string small, even for watersheds with thousands of reaches.
    # Preserve the original ID order while avoiding redundant requests.
    comids = list(dict.fromkeys(comids))
    if not comids:
        raise NoStreamsFoundException("No COMIDs supplied for NWM return period retrieval.")

    header = {'x-api-key': nwm_api_key}
    return_period_df = pd.concat([
        _request_nwm_rp_batch(comids[start:start + NWM_RP_BATCH_SIZE], header)
        for start in range(0, len(comids), NWM_RP_BATCH_SIZE)
    ], ignore_index=True)
    
    return_period_df = return_period_df.set_index("feature_id")
    return_period_df.index.name = "river_id"
    return_period_df.columns = ['rp2', 'rp5', 'rp10', 'rp25', 'rp50', 'rp100']

    # Add derived flows directly to rp_df without dropping anything
    return_period_df["rp100_premium"] = (return_period_df["rp100"] * 1.5) + 50

    # Reorder columns so the return period fields come first
    cols = [col for col in return_period_df.columns if col.startswith("rp")]
    return_period_df = return_period_df[cols]

    return_period_df['COMID'] = return_period_df.index

    # Reorder the DataFrame
    columns = ['COMID'] + [col for col in return_period_df.columns if col != 'COMID']
    return_period_df = return_period_df[columns]

    return return_period_df

def make_reanalysis_file(workspace: Workspace) -> Path:
    """
    This function generates a CSV file containing base and maximum flow values for each stream segment in the domain, based on the stream geometry and precomputed flow datasets. The flow values are derived from both the Flow Duration Curve (FDC) and Return Period (RP) datasets, which are accessed via Dask arrays for efficient computation. The resulting CSV file includes columns for various return periods and exceedance probabilities, as well as "premium" flow values calculated as 1.5 times the base flow plus 50.
    This is inspired by nencarta's equivalent function.
    """
    configs = workspace.configs
    if configs.reanalysis_file:
        workspace.DEM_Reanalsyis_FlowFile = Path(configs.reanalysis_file)
        return workspace.DEM_Reanalsyis_FlowFile

    if workspace.DEM_Reanalsyis_FlowFile.exists() and not configs.overwrite:
        return workspace.DEM_Reanalsyis_FlowFile
    
    if not workspace.DEM_StrmShp.exists() and not configs.raise_errors_if_nothing_in_domain:
        return None

    workspace.DEM_Reanalsyis_FlowFile.parent.mkdir(parents=True, exist_ok=True)
    stream_df = Vector(workspace.DEM_StrmShp, not workspace.configs.parallel).to_geopandas()

    river_ids = stream_df[configs.stream_id_field].astype(int).unique()

    if len(river_ids) == 0:
        LOG.error("No stream segments remain after filtering; cannot generate base/max flow file.")
        raise NoStreamsFoundException("After applying stream filters, no stream segments remain. Please adjust your stream filters or check your input stream geometry.")

    if configs.streamflow_source == StreamflowSource.GEOGLOWS:
        final_df = _get_geoglows_rp(river_ids)
    elif configs.streamflow_source.is_nwm():
        nwm_api_key = configs.nwm_api_key or os.getenv("NWM_API_KEY")
        if not nwm_api_key:
            raise ValueError("NWM_API_KEY environment variable must be set for NWM flow retrieval.")
        final_df = _get_nwm_rp(river_ids, nwm_api_key)
    else:
        raise ValueError("Invalid flow_source specified. Must be 'geoglows' or 'nwm'.")

    # break the code if the dataframe is empty or if the streamflow is all 0
    if final_df.empty or final_df[configs.specified_highflow_field].values.mean() <= 0:
        LOG.error(f"Results for {workspace.DEM_StrmShp} are not possible because we don't have streamflow estimates...")
        raise NoStreamsFoundException("No valid streamflow estimates found for the specified geometry.")

    LOG.info(final_df)

    if configs.q_baseflow_threshold:
        if configs.specified_bathyflow_field not in final_df.columns:
            LOG.warning(
                f"baseflow_threshold was provided ({configs.q_baseflow_threshold}), but baseflow field "
                f"'{configs.specified_bathyflow_field}' was not found in streamflow data. Skipping baseflow threshold filter."
            )
        else:
            final_df_before_filter_count = len(final_df)
            final_df = final_df[final_df[configs.specified_bathyflow_field] >= configs.q_baseflow_threshold]
            if final_df.empty:
                LOG.error(
                    f"All streams were removed by baseflow_threshold={configs.q_baseflow_threshold} "
                    f"using field '{configs.specified_bathyflow_field}'."
                )
                raise NoStreamsFoundException("After applying baseflow threshold filter, no stream segments remain. Please adjust your filter criteria.")
            
            LOG.info(
                f"Filtered out {final_df_before_filter_count - len(final_df)} streams below baseflow threshold of {configs.q_baseflow_threshold} using field '{configs.specified_bathyflow_field}'."
            )

    if workspace.DEM_Reanalsyis_FlowFile.suffix.endswith('.parquet'):
        final_df.round(3).to_parquet(workspace.DEM_Reanalsyis_FlowFile, index=False)
    else:
        final_df.round(3).to_csv(workspace.DEM_Reanalsyis_FlowFile, index=False)

    return workspace.DEM_Reanalsyis_FlowFile
