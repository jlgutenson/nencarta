import io
import json
import os
import shutil
import tempfile
import weakref
from functools import cache
from pathlib import Path

import fsspec
import numpy as np
import pandas as pd
import requests
import xarray as xr

from nencarta._constants import (
    GEOGLOWS_DAILY_URL,
    GEOGLOWS_FDC_URL,
    GEOGLOWS_RETURN_PERIODS_URL,
    NWM_RP_URL,
)
from nencarta.core.configs import NencartaConfig
from nencarta.core.enumerations import StreamflowSource
from nencarta.core.vector import Vector
from nencarta.exceptions import NoStreamsFoundException
from nencarta.logger import LOG
from nencarta.workspace import Workspace

# Exceedance probabilities (%) taken from the flow duration curve
FDC_EXCEEDANCES = np.array([*range(0, 101, 5), 1], dtype=float)

class MissingRiverIdsError(ValueError):
    """Some river IDs in the domain are not in a streamflow dataset."""


@cache
def get_daily_ds():
    """GEOGLOWS' daily retrospective flows."""
    return xr.open_zarr(GEOGLOWS_DAILY_URL, storage_options={'anon': True})

def _storage_options(source: str, configs: NencartaConfig) -> dict | None:
    """fsspec options for a remote source: reanalysis_storage_options, or anonymous access for S3."""
    if "://" not in source:
        return None
    if configs.reanalysis_storage_options is not None:
        return configs.reanalysis_storage_options
    return {"anon": True} if source.startswith("s3://") else {}

def _table_to_dataset(df: pd.DataFrame, dim: str, id_field: str) -> xr.Dataset:
    """
    Turn a wide table (a river ID column plus one rp<N> column per return period, or one
    p_exceed_<P> column per exceedance) into a Dataset indexed by river_id and ``dim`` with a
    single variable, 'flow'.
    """
    id_col = "river_id" if "river_id" in df.columns else id_field
    if id_col not in df.columns:
        raise ValueError(f"The table has neither a 'river_id' nor a '{id_field}' column.")
    
    prefix = "rp" if dim == "return_period" else "p_exceed_"
    columns = {}
    for col in df.columns:
        if isinstance(col, str) and col.startswith(prefix):
            try:
                columns[col] = float(col[len(prefix):])
            except ValueError:
                pass  # e.g. rp100_premium

    if not columns:
        raise ValueError(f"The table has no {prefix}<value> columns.")

    df = df.rename(columns={id_col: "river_id"}).melt(id_vars="river_id", value_vars=list(columns), var_name=dim, value_name="flow")
    df[dim] = df[dim].map(columns)
    return df.set_index(["river_id", dim]).to_xarray()

@cache
def _open_flow_dataset(source: str, dim: str, id_field: str, storage_options_json: str) -> xr.Dataset:
    """
    Open a return period (``dim='return_period'``) or flow duration curve (``dim='p_exceed'``)
    dataset from a local or remote CSV, Parquet, Zarr or NetCDF file, as a Dataset indexed by
    river_id and ``dim``. Cached so each process opens a dataset once.
    """
    storage_options = json.loads(storage_options_json)
    suffix = Path(source.rstrip("/")).suffix.lower()
    if suffix == ".zarr":
        ds = xr.open_zarr(source, storage_options=storage_options or None)
    elif suffix in {".nc", ".nc4", ".netcdf", ".cdf"}:
        local_source = source
        if "://" in source:
            # The netCDF4 engine only opens local files, so a remote file is downloaded first
            cache_dir = tempfile.mkdtemp(prefix="nencarta_flows_")
            protocol = source.split("://", 1)[0]
            local_source = fsspec.open_local(f"simplecache::{source}", **{protocol: storage_options or {}},
                                             simplecache={"cache_storage": cache_dir})
            
        ds = xr.open_dataset(local_source)
        if local_source != source:
            weakref.finalize(ds, shutil.rmtree, cache_dir, True)
    elif suffix == ".csv":
        ds = _table_to_dataset(pd.read_csv(source, storage_options=storage_options), dim, id_field)
    elif suffix == ".parquet":
        ds = _table_to_dataset(pd.read_parquet(source, storage_options=storage_options), dim, id_field)
    else:
        raise ValueError(f"Cannot read streamflow dataset {source}: expected a .csv, .parquet, .zarr or .nc file.")

    if "river_id" not in ds.dims and id_field in ds.dims:
        ds = ds.rename({id_field: "river_id"})

    for required in ("river_id", dim):
        if required not in ds.dims:
            raise ValueError(f"Streamflow dataset {source} has no '{required}' dimension; it has {list(ds.dims)}.")
        
    return ds

def _open_flows(source: str, dim: str, configs: NencartaConfig) -> xr.Dataset:
    storage_options = _storage_options(str(source), configs)
    return _open_flow_dataset(str(source), dim, configs.stream_id_field, json.dumps(storage_options, sort_keys=True))

def _river_positions(ds: xr.Dataset, river_ids: np.ndarray) -> np.ndarray | None:
    """
    Where each of ``river_ids`` is along ``ds``'s river_id, -1 where it isn't there, or None if one of them is there
    more than once (or ``river_ids`` repeat one). Found by looking the dataset's IDs up among the domain's few.
    """
    index = ds.indexes.get("river_id")
    wanted = pd.Index(river_ids)
    if index is None or not wanted.is_unique:
        return None

    found = wanted.get_indexer(index.to_numpy())
    along = np.flatnonzero(found >= 0)
    if np.bincount(found[along], minlength=len(wanted)).max(initial=0) > 1:
        return None

    positions = np.full(len(wanted), -1, np.int64)
    positions[found[along]] = along
    return positions

def _select_rivers(ds: xr.Dataset | xr.DataArray, river_ids: np.ndarray, **others) -> xr.Dataset | xr.DataArray:
    """``ds.sel(river_id=river_ids, **others)``, by position where it can be (see _river_positions)."""
    positions = _river_positions(ds, river_ids)
    if positions is None or (positions < 0).any():
        return ds.sel(river_id=river_ids, **others)
    return ds.sel(**others).isel(river_id=positions)

def _river_ids_in(ds: xr.Dataset, river_ids: np.ndarray, source: str, configs: NencartaConfig) -> np.ndarray:
    """Return the river IDs that ``ds`` has, or raise if some are missing and raise_errors_if_river_ids_missing is set."""
    positions = _river_positions(ds, river_ids)
    if positions is None:
        found = np.isin(river_ids, ds["river_id"].values)
    else:
        found = positions >= 0
        
    if found.all():
        return river_ids

    missing = river_ids[~found]
    message = f"{len(missing)} of {len(river_ids)} river IDs in the domain are not in {source} (e.g. {missing[:5].tolist()})"
    if configs.raise_errors_if_river_ids_missing:
        raise MissingRiverIdsError(f"{message}. Set 'raise_errors_if_river_ids_missing' to False to drop them.")
    
    LOG.warning(f"{message}; dropping them.")
    return river_ids[found]

def _variables_with_dims(ds: xr.Dataset, dim: str) -> list[str]:
    return [name for name, var in ds.data_vars.items() if set(var.dims) == {"river_id", dim}]

def _return_period_flows(rp_ds: xr.Dataset, river_ids: np.ndarray, source: str, configs: NencartaConfig) -> pd.DataFrame:
    # By default, every variable of river and return period, e.g. GEOGLOWS' gumbel, gumbel_hourly and gumbel_daily
    variables = configs.return_period_variables or _variables_with_dims(rp_ds, "return_period")
    missing = [variable for variable in variables if variable not in rp_ds.data_vars]
    if not variables or missing:
        problem = f"has no variable {', '.join(map(repr, missing))}" if missing else "has no variables of river_id and return_period"
        raise ValueError(
            f"Return period dataset {source} {problem}; "
            f"set 'return_period_variables' to some of {_variables_with_dims(rp_ds, 'return_period')}."
        )
    
    rp_df = _select_rivers(rp_ds[variables], river_ids).to_dataframe().reset_index()

    # Where there are several variables, use the highest flow
    rp_df['return_period_flow'] = rp_df[variables].max(axis=1).round(3)

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
    return rp_df

def _fdc_flows(fdc_ds: xr.Dataset, river_ids: np.ndarray, source: str, configs: NencartaConfig) -> pd.DataFrame:
    variable = configs.fdc_variable
    candidates = _variables_with_dims(fdc_ds, "p_exceed")
    if variable is None:
        # GEOGLOWS' FDC has several curves; its annual curve of hourly flows is the one used by default
        if "hourly_annual" in candidates:
            variable = "hourly_annual"
        elif len(candidates) == 1:
            variable = candidates[0]

    if variable is None or variable not in fdc_ds.data_vars:
        problem = f"has no variable {variable!r}" if variable else f"has {'several' if candidates else 'no'} variables of river_id and p_exceed"
        raise ValueError(f"Flow duration curve dataset {source} {problem}; set 'fdc_variable' to one of {candidates}.")

    missing = [f"{p:g}" for p in FDC_EXCEEDANCES if p not in fdc_ds.indexes["p_exceed"]]
    if missing:
        raise ValueError(f"Flow duration curve dataset {source} has no flows for exceedance(s) {', '.join(missing)}%.")

    fdc_df = _select_rivers(fdc_ds[variable], river_ids, p_exceed=FDC_EXCEEDANCES).to_dataframe().reset_index()
    fdc_df = fdc_df.pivot_table(
        index='river_id',
        columns='p_exceed',
        values=variable,
        aggfunc='mean'
    )
    return fdc_df.rename(columns={p: f"p_exceed_{p:g}" for p in fdc_df.columns})

def _fdc_flows_from_daily(daily_ds: xr.Dataset, river_ids: np.ndarray) -> pd.DataFrame:
    daily_df = _select_rivers(daily_ds, river_ids).to_dataframe().reset_index()

    # creating exceedance percentiles with the daily data
    quantiles = [1.0 - (p / 100.0) for p in FDC_EXCEEDANCES]
    fdc_df = daily_df.groupby('river_id')['Q'].quantile(quantiles).unstack()
    fdc_df = fdc_df.rename(
        columns={q: f"p_exceed_{p:g}" for q, p in zip(quantiles, FDC_EXCEEDANCES)}
    )

    # uniqify the index
    return fdc_df[~fdc_df.index.duplicated(keep='first')]

def _get_geoglows_rp(river_ids: np.ndarray, configs: NencartaConfig) -> pd.DataFrame:
    """
    The return period flows (rp<N>) and, with include_fdc, flow duration curve flows (p_exceed_<P>) of each river,
    plus rp100_premium and p_exceed_0_premium: GEOGLOWS V2's, or return_period_file's and fdc_file's.
    """
    fdc_source = (configs.fdc_file or GEOGLOWS_FDC_URL) if configs.include_fdc else None
    return _return_period_and_fdc_flows(river_ids, configs, configs.return_period_file or GEOGLOWS_RETURN_PERIODS_URL,
                                        fdc_source)

def _get_custom_rp(river_ids: np.ndarray, configs: NencartaConfig) -> pd.DataFrame:
    """As _get_geoglows_rp, from return_period_file and, with include_fdc, fdc_file only."""
    fdc_source = configs.fdc_file if configs.include_fdc else None
    return _return_period_and_fdc_flows(river_ids, configs, configs.return_period_file, fdc_source)

def _return_period_and_fdc_flows(river_ids: np.ndarray, configs: NencartaConfig, rp_source: str,
                                 fdc_source: str | None) -> pd.DataFrame:
    """
    The return period flows of rp_source and, if given, the flow duration curve flows of fdc_source, plus the
    premium high flows. GEOGLOWS' own FDC falls back to its daily flows when it can't be read.
    """
    rp_ds = _open_flows(rp_source, "return_period", configs)
    river_ids = _river_ids_in(rp_ds, river_ids, rp_source, configs)

    fdc_df = None
    if fdc_source:
        try:
            fdc_ds = _open_flows(fdc_source, "p_exceed", configs)
            river_ids = _river_ids_in(fdc_ds, river_ids, fdc_source, configs)
            fdc_df = _fdc_flows(fdc_ds, river_ids, fdc_source, configs)
        except MissingRiverIdsError:
            raise
        except Exception:
            if fdc_source != GEOGLOWS_FDC_URL:
                raise
            LOG.warning("FDC data not available; falling back to daily data for FDC calculation.")
            daily_ds = get_daily_ds()
            river_ids = _river_ids_in(daily_ds, river_ids, GEOGLOWS_DAILY_URL, configs)
            fdc_df = _fdc_flows_from_daily(daily_ds, river_ids)

    rp_df = _return_period_flows(rp_ds, river_ids, rp_source, configs)
    final_df = pd.concat([fdc_df, rp_df], axis=1) if fdc_df is not None else rp_df.copy()
    final_df['COMID'] = final_df.index

    # Reorder the DataFrame
    columns = ['COMID'] + [col for col in final_df.columns if col != 'COMID']
    final_df = final_df[columns]

    for col in ['p_exceed_0', 'rp100']:
        if col not in final_df.columns:
            continue
        # A high flow for the rating curves to reach: 1.5 times the flow, plus 50 m3/s for rivers whose 100 year flow
        # is close to 0. Much higher would leave bigger gaps between the VDT's flows, and less accurate curves.
        final_df[f'{col}_premium'] = (final_df[col] * 1.5) + 50

    final_df = final_df.round(3)
    return final_df

def _get_nwm_rp(comids: list[int], nwm_api_key: str):
    """The NWM return period flows (rp<N>) of each river from the NWM API, plus rp100_premium."""
    if not nwm_api_key:
        raise ValueError("nwm_api_key is required for NWM return period requests.")

    header = {'x-api-key': nwm_api_key}
    params = {'comids': ','.join(map(str, comids)),
              'output_format': 'csv',
              'order_by_comid': False,}

    response = requests.get(NWM_RP_URL, params=params, headers=header, timeout=60)

    if response.status_code == 200:
        return_period_df = pd.read_csv(io.StringIO(response.text))
    else:
        raise requests.exceptions.HTTPError(response.text)
    
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
    Write the flow statistics of each reach in the domain (workspace.DEM_Reanalsyis_FlowFile): its return period
    flows and, for GEOGLOWS with include_fdc, its flow duration curve flows, plus the "premium" high flows. 
    Reaches whose baseflow is below q_baseflow_threshold are left out. When
    reanalysis_file is given, it is used instead.
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

    if configs.streamflow_source == StreamflowSource.GEOGLOWS_V2:
        final_df = _get_geoglows_rp(river_ids, configs)
    elif configs.streamflow_source == StreamflowSource.CUSTOM:
        final_df = _get_custom_rp(river_ids, configs)
    elif configs.streamflow_source.is_nwm():
        if configs.return_period_file or configs.fdc_file:
            LOG.warning("'return_period_file' and 'fdc_file' are only used with GEOGLOWS; NWM return periods come from the NWM API.")
        nwm_api_key = configs.nwm_api_key or os.getenv("NWM_API_KEY")
        if not nwm_api_key:
            raise ValueError("NWM_API_KEY environment variable must be set for NWM flow retrieval.")
        final_df = _get_nwm_rp(river_ids, nwm_api_key)
    else:
        raise ValueError(f"Unknown streamflow_source {configs.streamflow_source}.")

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
