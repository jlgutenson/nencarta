
from nencarta.core.enumerations import FloodMapMode
from nencarta.core.model_config import ModelConfig
from nencarta.tasks import (
    Create_BaseLine_Manning_n_File_ESA,
    assign_and_validate_dem,
    assign_user_flow_files,
    burn_streams_and_move_streams,
    define_arc_configs,
    define_configs_for_dem_cleaning,
    define_mapper_bathymetry_config,
    define_mapper_configs,
    get_fist_inputs,
    make_clean_dem,
    make_depth_mask,
    make_flood_flow_file_from_base_max_file,
    make_flow_file_from_forecast,
    make_land_cover,
    make_outlet_wse_file,
    make_reanalysis_file,
    make_return_period_flow_file,
    make_stream_geometry,
    make_stream_raster,
    make_water_mask,
    remove_old_forecast_files,
)
from nencarta.workspace import Workspace


def prepare_inputs_for_dem(workspace: Workspace) -> ModelConfig:
    """
    Make every input one DEM needs, in order: the DEM, Manning's n, land cover, streams, flow statistics, stream
    raster, water mask, burned and moved streams, cleaned DEM, flow files, and the ARC and flood mapper configs
    (with use_specified_depth_for_bathy_mask, first ARC's water mask, the streams flooded to a set depth; with
    outlet_exchange pass2, the outlets' water from the neighbouring DEMs). Files that exist are reused unless
    overwrite. Returns the configs.
    """
    DEM = workspace.assigned_dem
    configs = workspace.configs
    if not DEM.suffix.lower() in {'.tif', '.vrt', '.img', '.tiff', '.btiff', '.bil', '.dem'}:
        return ModelConfig(None, [], [], configs.mapper, configs.quiet)

    assign_and_validate_dem(workspace)
    Create_BaseLine_Manning_n_File_ESA(workspace)
    make_land_cover(workspace)
    make_stream_geometry(workspace)
    make_reanalysis_file(workspace)
    make_flood_flow_file_from_base_max_file(workspace)

    if not configs.move_stream_network_to_thalweg or not configs.disable_bathymetry:
        make_stream_raster(workspace)

    if configs.clean_dem or not configs.disable_bathymetry or configs.burn_streams or configs.move_stream_network_to_thalweg \
            or configs.flood_bathy_mask:
        make_water_mask(workspace)


    if configs.move_stream_network_to_thalweg or configs.burn_streams:
        burn_streams_and_move_streams(workspace)

    if configs.clean_dem:
        define_configs_for_dem_cleaning(workspace)
        make_clean_dem(workspace)
        
    if configs.floodmap_mode == FloodMapMode.FORECAST:
        flow_files = make_flow_file_from_forecast(workspace)
        if configs.remove_old_forecast_files:
            remove_old_forecast_files(workspace)
    elif configs.floodmap_mode == FloodMapMode.USER:
        flow_files = assign_user_flow_files(configs)
    elif configs.floodmap_mode == FloodMapMode.RETURN_PERIOD:
        flow_files = make_return_period_flow_file(workspace)
    else:
        flow_files = []

    if configs.use_specified_depth_for_bathy_mask and not configs.disable_bathymetry:
        # On the DEM ARC runs on, so after the burn and the DEM cleaner
        make_depth_mask(workspace)

    if configs.outlet_exchange == "pass2":
        make_outlet_wse_file(workspace)

    arc_config = define_arc_configs(workspace)
    bathy_mapper_config = define_mapper_bathymetry_config(workspace)

    mapper_configs = []
    fist_inputs = []
    for flow_file in flow_files:
        mapper_configs.append(define_mapper_configs(workspace, flow_file))
        if configs.make_fist_inputs:
            fist_inputs.extend(get_fist_inputs(workspace, flow_file))

    return ModelConfig(
        arc_config,
        mapper_configs,
        fist_inputs,
        configs.mapper,
        configs.quiet,
        bathy_mapper_config=bathy_mapper_config)