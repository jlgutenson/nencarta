from pathlib import Path

import pandas as pd

from nencarta.core.enumerations import FloodMapMode
from nencarta.core.model_config import ModelConfig
from nencarta.logger import LOG
from nencarta.workspace import Workspace


def get_fist_inputs(workspace: Workspace, flow_file: Path) -> list[tuple]:
    """
    The arguments for ARC's Curve-to-GeoJSON program, one set per flow column of a forecast or user flow file.
    Each writes a GeoJSON of the water surface elevation at each stream cell, for FIST, from ARC's curve file, and
    marks the headwater (Seed) cells. A flow file's GeoJSONs are only remade when some are missing, or with
    overwrite. None are made without a curve file (make_curvefile), or in return_period mode.
    """
    OutProjection = "EPSG:4269"
    configs = workspace.configs
    if not configs.make_curvefile:
        LOG.warning("FIST inputs are made from ARC's curve file: set 'make_curvefile' to True to make them.")
        return []

    # The Seed file, which ARC makes once and then reuses, is shared by every forecast, but made per user flow file
    if configs.floodmap_mode == FloodMapMode.FORECAST:
        SEED_File = workspace.file_path(workspace.FIST_Folder, 'Seed', 'parquet')
        date = configs.forensic_forecast_date or configs.forecastdate
        hour = configs.forensic_forecast_hour if configs.forensic_forecast_date else configs.forecasthour
        flows_name = f"_{date}_{hour}" if configs.streamflow_source.is_nwm() else f"_{date}"
    elif configs.floodmap_mode == FloodMapMode.USER:
        flow_name = flow_file.name.rsplit('.', 1)[0]
        SEED_File = workspace.file_path(workspace.FIST_Folder, 'Seed', 'parquet', suffix=f"_{flow_name}",
                                        legacy=f"{workspace.FileName}_{flow_name}_Seed.parquet")
        flows_name = f"_{flow_name}"
    else:
        LOG.info(f"FIST inputs are made for forecasts and user flow files only, not in {configs.floodmap_mode} mode.")
        return []

    streamflow_forecast_df = pd.read_csv(flow_file)

    # grab the stream id column, which should be the first column in the flow file and should be named 'rivid' or 'comid' depending on the source of streamflow data
    id_column_name = streamflow_forecast_df.columns[0]
    # Every other numeric column is a flow, whole numbers included (they're read as integers)
    streamflow_columns = [column for column in streamflow_forecast_df.columns[1:]
                          if pd.api.types.is_numeric_dtype(streamflow_forecast_df[column])]

    geojson_files = {}
    for streamflow_column in streamflow_columns:
        geojson_suffix = f"{flows_name}_{streamflow_column}"
        geojson_files[streamflow_column] = workspace.file_path(workspace.FIST_Folder, 'FIST', 'geojson', suffix=geojson_suffix,
                                                               legacy=f"{workspace.FileName}{geojson_suffix}.geojson")
    if all(path.exists() for path in geojson_files.values()) and not configs.overwrite:
        LOG.info(f"The FIST inputs of {flow_file} already exist and we aren't making them again...")
        return []
    if configs.overwrite:
        SEED_File.unlink(missing_ok=True)
    workspace.FIST_Folder.mkdir(parents=True, exist_ok=True)

    # ARC joins these paths to text, so they are given as strings
    tasks = []
    for streamflow_column, GeoJSON_File in geojson_files.items():
        GeoJSON_File.unlink(missing_ok=True)
        tasks.append((
            str(workspace.Curve_File_Bathy),
            str(workspace.model_dem),
            str(GeoJSON_File),
            OutProjection,
            str(workspace.model_StrmShp),
            configs.stream_id_field,
            configs.downstream_id_field,
            str(SEED_File),
            True,
            None,
            streamflow_forecast_df[[id_column_name, streamflow_column]]
        ))

    return tasks

def run_fist(model_config: ModelConfig, flag = None) -> None:
    """Write the FIST GeoJSON files. ``flag`` is only there so the pipeline runs this after ARC."""
    from arc import Run_Main_Curve_to_GEOJSON_Program_Stream_Vector
    for args in model_config.fist_inputs:
        Run_Main_Curve_to_GEOJSON_Program_Stream_Vector(*args)
