from pathlib import Path

import pandas as pd

from nencarta.logger import LOG
from nencarta.workspace import Workspace


def _make_flood_flow_file_from_base_max_file(reanalysis_flow_file: Path,
                                            out_file: Path,
                                            columns: list[str]) -> Path:
    """Write ``columns`` of the flow statistics file to ``out_file``."""
    df = pd.read_parquet(reanalysis_flow_file) if reanalysis_flow_file.suffix.endswith('.parquet') else pd.read_csv(reanalysis_flow_file)
    df = df.reset_index()[columns]
    df.to_parquet(out_file, index=False, compression='brotli') if out_file.suffix.endswith('.parquet') else df.to_csv(out_file, index=False)


def make_flood_flow_file_from_base_max_file(workspace: Workspace) -> Path:
    """Write each reach's baseflow (specified_bathyflow_field) to workspace.COMID_Q_File, for the bathymetry run."""
    if workspace.COMID_Q_File.exists() and not workspace.configs.overwrite:
        LOG.info(f"{workspace.COMID_Q_File} already exists and we aren't making it again...")
        return workspace.COMID_Q_File
    
    if not workspace.DEM_Reanalsyis_FlowFile.exists() and not workspace.configs.raise_errors_if_nothing_in_domain:
        return None
    
    workspace.COMID_Q_File.parent.mkdir(parents=True, exist_ok=True)

    LOG.info("Creating flood flow file from reanalysis file...")
    columns = ['COMID', workspace.configs.specified_bathyflow_field]
    _make_flood_flow_file_from_base_max_file(workspace.DEM_Reanalsyis_FlowFile, workspace.COMID_Q_File, columns)

    return workspace.COMID_Q_File
