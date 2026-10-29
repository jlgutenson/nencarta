import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from nencarta import download_forecasts as ForecastFlows
from nencarta.core.configs import NencartaConfig
from nencarta.core.vector import Vector
from nencarta.logger import LOG
from nencarta.tasks.configs import flood_map_paths, forecast_postfix
from nencarta.workspace import Workspace

# The date, and for NWM the hour, of the forecast a flood map's name says it was made for
_FORECAST_IN_NAME = re.compile(r"Forecast_(\d{8})(?:_([^._]+))?")

def _forecast_flow_file(workspace: Workspace, forecastdate, forecasthour) -> Path:
    """The forecast flow file of one forecast."""
    configs = workspace.configs
    when = f"_{forecastdate}_{forecasthour}" if configs.streamflow_source.is_nwm() else f"_{forecastdate}"
    return workspace.file_path(workspace.FLOW_Folder, 'forecast', 'csv', suffix=when,
                               legacy=f"{workspace.FileName}{when}_{configs.streamflow_source.file_prefix}_forecast.csv")

def make_flow_file_from_forecast(workspace: Workspace) -> list[Path]:
    """
    Download the forecast flows of the domain's reaches: the forecast of forensic_forecast_date, or the newest one
    of the last 12 days. Returns the forecast flow file.
    """
    configs = workspace.configs
    rivids = Vector(workspace.model_StrmShp, not workspace.configs.parallel).to_geopandas(columns=[configs.stream_id_field])[configs.stream_id_field].astype(int).tolist()
    
    forecastdate = configs.forensic_forecast_date
    forecasthour = configs.forensic_forecast_hour
    if forecastdate:
        LOG.info(f"Using forensic forecast date: {forecastdate}")
        if configs.streamflow_source.is_nwm():
            LOG.info(f"Using forensic forecast hour: {forecasthour}")
        else:
            forecasthour = None
        
        try:
            ForecastFlowFile = _forecast_flow_file(workspace, forecastdate, forecasthour)
            if not ForecastFlowFile.exists() or configs.overwrite:
                ForecastFlows.Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, ForecastFlowFile, configs.streamflow_source, configs.nwm_api_key)
        except Exception:
            LOG.error('Could not process forensic forecast streamflow download, please check your date or try again later...')
            raise
    else:
        # cycle through today through 12 days ago to find the most recent day with a forecast
        found = False
        for fd in range(13):
            for fh in range(24):
                try:
                    forecastdate, forecasthour = ForecastFlows.Get_Date_For_Forecast(fd, fh, configs.streamflow_source) 
                    LOG.info(f"Attempting to download forecast for date: {forecastdate} and hour: {forecasthour}...")
                    # we only need the forecast date for GEOGLOWS, for NWM we need the forecast hour as well               
                    ForecastFlowFile = _forecast_flow_file(workspace, forecastdate, forecasthour)
                    if not ForecastFlowFile.exists() or configs.overwrite:
                        ForecastFlows.Process_and_Write_Forecast_Data(forecastdate, forecasthour, rivids, ForecastFlowFile, configs.streamflow_source, configs.nwm_api_key)

                    if configs:
                        configs.forecastdate = forecastdate
                        configs.forecasthour = forecasthour

                    found = True
                    break
                except Exception as e:  # noqa: BLE001
                    LOG.error(f'Could not process forecast, moving back another day.. ({e})')

            if found:
                break  # break outer
            else:
                LOG.error('We are sorry, the forecast could not be found within the bounds of our current logic.')

    LOG.info(f'Forecast data save here: {ForecastFlowFile}')    
    return [ForecastFlowFile]

def assign_user_flow_files(configs: NencartaConfig,) -> list[Path]:
    """The user flow files, as paths."""
    flow_files = []
    if configs.user_flow_files:
        for file in configs.user_flow_files:
            flow_files.append(Path(file))
    return flow_files

def remove_old_forecast_files(workspace: Workspace) -> None:
    """
    Delete this DEM's forecast flood maps, and the files named after them (such as a depth map's WGS84 copy), whose
    forecast was issued at least age_of_forecast_days ago (UTC). The forecast being mapped is kept.
    """
    configs = workspace.configs
    if not workspace.flood_folder.is_dir():
        return
    current = configs.forensic_forecast_date or configs.forecastdate
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=configs.age_of_forecast_days)
    for path in sorted(workspace.flood_folder.iterdir()):
        match = _FORECAST_IN_NAME.search(path.name)
        if match is None or match[1] == current or not path.is_file():
            continue
        try:
            issued = datetime.strptime(match[1], "%Y%m%d").date()
        except ValueError:
            continue
        if issued > cutoff:
            continue
        # Only this DEM's maps of that forecast, named as the mapper configs name them, and the files named after them
        flood_maps = flood_map_paths(workspace, forecast_postfix(configs, match[1], match[2]))
        if any(path.name.startswith(f"{flood_map.stem}{separator}") for flood_map in flood_maps.values() for separator in "._"):
            path.unlink()
            LOG.info(f"Deleted the old forecast file {path}.")

def make_return_period_flow_file(workspace: Workspace) -> list[Path]:
    """Write a flow file for each of return_periods, from the rp<N> column of the flow statistics. GEOGLOWS only."""
    configs = workspace.configs
    if configs.streamflow_source.is_nwm():
        LOG.error("Return period flow file generation is not currently supported for NWM streamflow source.")
        raise NotImplementedError("Return period flow file generation is not currently supported for NWM streamflow source.")
    
    if not workspace.DEM_Reanalsyis_FlowFile.exists():
        if configs.raise_errors_if_nothing_in_domain:
            raise FileNotFoundError(f"Reanalysis flow file {workspace.DEM_Reanalsyis_FlowFile} does not exist. Cannot create return period flow files.")
        else:
            return []
    
    files = []
    for rp in configs.return_periods:
        return_period_flow_file = workspace.return_period_flow_file(rp)
        files.append(return_period_flow_file)

        if return_period_flow_file.exists() and not configs.overwrite:
            LOG.info(f"{return_period_flow_file} already exists and we aren't making it again...")
            continue
        
        LOG.info(f"Creating return period flow file for rp{rp} at {return_period_flow_file}...")
        pd.read_csv(workspace.DEM_Reanalsyis_FlowFile)[['COMID', f"rp{rp}"]].to_csv(return_period_flow_file, index=False)

    return files
