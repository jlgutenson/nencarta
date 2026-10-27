import subprocess
from pathlib import Path

from nencarta.core.raster import Raster
from nencarta.logger import LOG
from nencarta.workspace import Workspace
from nencarta.core.enumerations import Mapper
from nencarta.core.configs import NencartaConfig
from nencarta._constants import FLOODSPREADER_PATH
from nencarta.core.inspect_config import inspect_config
from nencarta.core.floodmapper_output import FloodMapperBulkOutput, FloodMapperOutput
from nencarta.core.model_config import ModelConfig
from nencarta.tasks.move_streams import load_lake_array

def _run_arc(config: Path, model_config: ModelConfig) -> None:
    from arc import Arc # Lazy import helps load faster
    Arc(str(config), quiet=model_config.quiet).run()

def _run_mapper(config_file: Path, model_config: ModelConfig) -> None:
    if model_config.mapper == Mapper.FLOODSPREADER:
        if not FLOODSPREADER_PATH.exists():
            raise FileNotFoundError(f"FloodSpreader script not found at {FLOODSPREADER_PATH}. Please ensure it is included in the nencarta package.")
        call_mapper = f'python "{FLOODSPREADER_PATH}" {config_file}'
        subprocess.call(call_mapper, shell=True)
    else:
        from curve2flood import Curve2Flood_MainFunction
        Curve2Flood_MainFunction(str(config_file), quiet=model_config.quiet)

def run_arc_bathymetry(model_config: ModelConfig, workspace: Workspace) -> ModelConfig:
    if model_config.arc_config is None:
        LOG.info("No ARC config was generated for this workspace; skipping ARC bathymetry.")
        return model_config

    if model_config.vdt_exists and not workspace.configs.overwrite:
        return model_config
    
    if not workspace.DEM_StrmShp.exists() and not workspace.configs.raise_errors_if_nothing_in_domain:
        return model_config
    
    LOG.info("Running ARC...")
    _run_arc(model_config.arc_config, model_config)
    
    return model_config

def run_mapper_bathymetry(model_config: ModelConfig, workspace: Workspace) -> ModelConfig:
    if not model_config.vdt_exists or\
          workspace.configs.disable_bathymetry or \
            (model_config.burned_dem_exists and not workspace.configs.overwrite):
        return model_config
    
    LOG.info("Running flood mapper to generate bathymetry...")
    _run_mapper(model_config.arc_config, model_config)

    return model_config

def run_mapper_floodmaps(model_config: ModelConfig, workspace: Workspace) -> FloodMapperBulkOutput:
    if not workspace.DEM_StrmShp.exists() and not workspace.configs.raise_errors_if_nothing_in_domain:
        return FloodMapperBulkOutput([])
    
    LOG.info("Running flood mapper to generate flood maps...")
    for config in model_config.mapper_configs:
        if model_config.vdt_exists and not _mapper_has_required_outputs(config, workspace):
            _run_mapper(config, model_config)
            
    return FloodMapperBulkOutput(model_config.mapper_configs)

def run_arc_for_initial_floodmap(config: Path, configs: NencartaConfig) -> Path:
    LOG.info("Running ARC to generate initial floodmap for DEM cleaner...")
    _run_arc(config, configs)
    
    return config

def _mapper_has_required_outputs(config: Path, workspace: Workspace) -> bool:
    if workspace.configs.overwrite_floodmaps:
        return False
    
    required_outputs = ['OutFLD']
    configs = workspace.configs
    if configs.make_depth_maps:
        required_outputs.append('OutDEP')
    if configs.make_velocity_maps:
        required_outputs.append('OutVEL')
    if configs.make_wse_maps:
        required_outputs.append('OutWSE')

    return all(Path(inspect_config(config, output_key)).exists() for output_key in required_outputs)

def run_fldpln_library(model_config: ModelConfig, workspace: Workspace) -> ModelConfig:
    if not workspace.mapper.is_curve2flood_fldpln_mapper() or \
        (workspace.fldpln_library.exists() and not workspace.configs.overwrite):
        return model_config
    
    if (not workspace.DEM_StrmShp.exists() or not workspace.stream_info_file.exists() or not workspace.VDT_File_Bathy.exists()):
        if not workspace.configs.raise_errors_if_nothing_in_domain:
            return model_config
        if not workspace.DEM_StrmShp.exists():
            raise FileNotFoundError(f"Stream shapefile not found at {workspace.DEM_StrmShp}. Cannot build FLDPLN library.")
        if not workspace.stream_info_file.exists():
            raise FileNotFoundError(f"Stream info file not found at {workspace.stream_info_file}. Cannot build FLDPLN library.")
        if not workspace.VDT_File_Bathy.exists():
            raise FileNotFoundError(f"VDT file not found at {workspace.VDT_File_Bathy}. Cannot build FLDPLN library.")
    
    if workspace.fldpln_library.exists():
        workspace.fldpln_library.unlink()

    if not workspace.configs.disable_bathymetry:
        dem = workspace.FS_BathyFile
    elif workspace.configs.burn_streams:
        dem = workspace.fixed_dem
    elif workspace.configs.clean_dem:
        dem = workspace.DEM_File_Clean
    else:
        dem = workspace.assigned_dem

    bg_mask = None
    if workspace.configs.lakes:
        bg_mask = load_lake_array(workspace, Raster(dem))

    LOG.info("Building FLDPLN library...")
    from curve2flood import build_fldpln_library

    build_fldpln_library(
        filled_dem = workspace.filled_dem,
        stream_info_file = workspace.stream_info_file,
        flow_direction_file = workspace.flowdir,
        library_file = workspace.fldpln_library,
        dh = workspace.configs.fldpln_dh,
        fldmn = workspace.configs.fldpln_min_depth,
        fldmx = workspace.configs.fldpln_max_depth,
        iterative_spill = workspace.configs.fldpln_keep_spilling,
        vdt_file = workspace.VDT_File_Bathy,
        parallel = workspace.configs.fldpln_parallel,
        pbar = not workspace.configs.quiet,
        bg_mask=bg_mask,
        solver=workspace.configs.fldpln_solver
    )

    return model_config
