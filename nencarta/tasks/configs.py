import io
from pathlib import Path

import yaml

from nencarta.logger import LOG
from nencarta.workspace import Workspace
from nencarta.core.defaults import DEFAULT_BATHY_ARGS, DEFAULT_CONFIG, DEFAULT_FLOODMAP_ARGS
from nencarta.core.enumerations import FloodMapMode, Mapper, StreamflowSource

def _arc_inputs(dem: Path,
                stream_raster: Path,
                land_cover: Path,
                mannings_n_file: Path,
                reanalysis_flow_file: Path,
                river_id: str,
                highflow_field: str,
                baseflow_field: str,
                bathy_args: dict,
                include_baseflow: bool = True,
                ):
    # Keep key names + ordering aligned with the legacy configs produced by main.py.
    params = {
        "#ARC_Inputs": "",
        "DEM_File": dem,
        "Stream_File": stream_raster,
        "LU_Raster_SameRes": land_cover,
        "LU_Manning_n": mannings_n_file,
        "Flow_File": reanalysis_flow_file,
        "Flow_File_ID": river_id,
    }
    if include_baseflow:
        params["Flow_File_BF"] = baseflow_field
    params["Flow_File_QMax"] = highflow_field
    params["Spatial_Units"] = "deg"

    optional_bathy_keys = [
        "X_Section_Dist",
        "Degree_Manip",
        "Degree_Interval",
        "Low_Spot_Range",
        "Str_Limit_Val",
        "Gen_Dir_Dist",
        "Gen_Slope_Dist",
        "Stream_Slope_Method",
    ]

    params.update({
        key: bathy_args[key]
        for key in optional_bathy_keys
        if key in bathy_args
    })

    return params

def _fldpln_inputs(workspace: Workspace):
    output = {
        "# FLDPLN_Inputs": "",
        "Flow_Direction_File": workspace.flowdir,
        "Filled_DEM_File": workspace.filled_dem,
        "Stream_Info_File": workspace.stream_info_file,
        "FLDPLN_Library": workspace.fldpln_library,
        "reach_id": workspace.configs.stream_id_field,
        "downstream_reach_id": workspace.configs.downstream_id_field,
        "max_wse_rise": workspace.configs.fldpln_max_wse_rise,
    }
    if workspace.configs.floodmap_args.get("FLDPLN_Median_Filter_Size"):
        output.update({
            "FLDPLN_Median_Filter_Size": workspace.configs.floodmap_args.get("FLDPLN_Median_Filter_Size"),
            "FLDPLN_Missing_FSP_Interpolation": workspace.configs.floodmap_args.get("FLDPLN_Missing_FSP_Interpolation"),
            "FLDPLN_DoF_Signal": workspace.configs.floodmap_args.get("FLDPLN_DoF_Signal"),
            "FLDPLN_Threshold_Mode": workspace.configs.floodmap_args.get("FLDPLN_Threshold_Mode"),
            "": "",
        })
    return output

def _write_config(config_path: Path, params: dict):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, 'w') as f:
        if config_path.suffix.lower() in {'.yaml', '.yml'}:
            stream = io.StringIO()
            # Convert all Paths in params to strings for YAML serialization
            params_serializable = {key: (str(value) if isinstance(value, (Path, Mapper)) else value) for key, value in params.items()}
            yaml.dump(params_serializable, stream, encoding='utf-8', sort_keys=False, Dumper=yaml.CDumper) # Use CDumper for faster dumping
            # if a line startwith "'#", then strip the quotation and make it a comment
            content = stream.getvalue()
            content = '\n'.join(line if not line.startswith("'#") else f"# {line[2:].split(':')[0][:-1]}" for line in content.split('\n'))
            # Replace lines starting with : or ? with ''
            content = '\n'.join(line if (not line.startswith((':', '?', "'"))) else '' for line in content.split('\n'))
            f.write(content)
        else:
            first_line = True
            for key, value in params.items():
                # Legacy config style: section headers are single tokens on their own line,
                # with a blank line separating sections.
                if isinstance(key, str) and key.startswith("#"):
                    if not first_line:
                        f.write("\n\n")
                    f.write(str(key))
                else:
                    if not first_line:
                        f.write("\n")
                    f.write(f"{key}\t{value}")
                first_line = False

def _has_required_arc_outputs(workspace: Workspace) -> bool:
    required_files = [workspace.VDT_File_Bathy]
    if workspace.Curve_File_Bathy:
        required_files.append(workspace.Curve_File_Bathy)
    if workspace.AP_File:
        required_files.append(workspace.AP_File)
    return all(path.exists() for path in required_files)
                
def define_configs_for_dem_cleaning(workspace: Workspace) -> Path:
    workspace.ARC_Folder.mkdir(parents=True, exist_ok=True)
    workspace.VDT_Folder.mkdir(parents=True, exist_ok=True)
    workspace.flood_folder.mkdir(parents=True, exist_ok=True)
    workspace.dem_updated_folder.mkdir(parents=True, exist_ok=True)

    configs = workspace.configs
    if workspace.ARC_FileName_for_DEM_Cleaner.exists() and not configs.overwrite:
        return workspace.ARC_FileName_for_DEM_Cleaner

    if configs.burn_streams:
        dem = workspace.fixed_dem
    else:
        dem = workspace.assigned_dem

    if configs.move_stream_network_to_thalweg:
        streamlines = workspace.new_StrmShp_matched
        stream_raster = workspace.new_stream_raster
    else:
        streamlines = workspace.DEM_StrmShp
        stream_raster = workspace.STRM_File_Clean

    params = _arc_inputs(
        dem=dem,
        stream_raster=stream_raster,
        land_cover=workspace.LAND_File,
        mannings_n_file=workspace.mannings_n_text_file,
        reanalysis_flow_file=workspace.DEM_Reanalsyis_FlowFile,
        river_id="COMID",
        highflow_field=configs.specified_highflow_field,
        baseflow_field=configs.specified_bathyflow_field,
        bathy_args=configs.bathy_args,
        include_baseflow=True,
    )
    
    params["reach_id"] = configs.stream_id_field
    params["downstream_reach_id"] = configs.downstream_id_field
    if configs.use_power_laws_for_bathymetry:
        params["drainage_area_field"] = configs.area_km2_field
        params["coefficient_depth"] = configs.coefficient_depth
        params["exponent_depth"] = configs.exponent_depth
        params["coefficient_width"] = configs.coefficient_width
        params["exponent_width"] = configs.exponent_width

    params["#VDT_Output_File_and_CurveFile"] = ""
    params["VDT_Database_NumIterations"] = configs.bathy_args.get("VDT_Database_NumIterations", DEFAULT_BATHY_ARGS["VDT_Database_NumIterations"])
    params["Print_VDT_Database"] = workspace.VDT_File_Initial
    params["Print_Curve_File"] = workspace.Curve_File_Initial
    params["Reach_Average_Curve_File"] = configs.create_reach_average_curve_file

    params["#Mapper Input Data"] = ""
    params["StrmShp_File"] = streamlines
    params["Comid_Flow_File"] = workspace.COMID_Q_File
    params["mapper"] = configs.mapper if not configs.mapper.is_curve2flood_fldpln_mapper() else Mapper.CURVE2FLOOD_KERNEL_WEIGHTED
    params["FS_ADJUST_FLOW_BY_FRACTION"] = configs.bathy_args.get("FS_ADJUST_FLOW_BY_FRACTION", DEFAULT_BATHY_ARGS["FS_ADJUST_FLOW_BY_FRACTION"])
    params["Bathy_Use_Banks"] = configs.bathy_use_banks
    if configs.flood_waterlc_and_strm_cells:
        params["Flood_WaterLC_and_STRM_Cells"] = True
        params["LAND_WaterValue"] = configs.land_watervalue
    if configs.find_banks_based_on_landcover:
        params["FindBanksBasedOnLandCover"] = True
    if configs.use_specified_depth_for_bathy_mask:
        params["OutFLD"] = workspace.FloodMapFile_Initial
        params["OutSHP"] = workspace.FloodMapFile_Initial_SHP
        params["FloodSpreader_SpecifyDepth"] = configs.specify_depths_for_bathy_mask[0]

    _write_config(workspace.ARC_FileName_for_DEM_Cleaner, params)
    return workspace.ARC_FileName_for_DEM_Cleaner

def define_arc_configs(workspace: Workspace,) -> Path:
    workspace.ARC_Folder.mkdir(parents=True, exist_ok=True)
    workspace.VDT_Folder.mkdir(parents=True, exist_ok=True)
    workspace.flood_folder.mkdir(parents=True, exist_ok=True)

    configs = workspace.configs
    if configs.disable_bathymetry and _has_required_arc_outputs(workspace) and not configs.overwrite:
        LOG.info(f"Domain {workspace.watershed} has required ARC outputs and bathymetry is disabled, skipping ARC.")
        return workspace.ARC_FileName_Bathy

    if configs.clean_dem:
        dem = workspace.DEM_File_Clean
    elif configs.burn_streams:
        dem = workspace.fixed_dem
    else:
        dem = workspace.assigned_dem

    if configs.move_stream_network_to_thalweg:
        stream_raster = workspace.new_stream_raster
        stream_vector = workspace.new_StrmShp_matched
    else: 
        stream_raster = workspace.STRM_File_Clean
        stream_vector = workspace.DEM_StrmShp


    params = _arc_inputs(
        dem=dem,
        stream_raster=stream_raster,
        land_cover=workspace.LAND_File,
        mannings_n_file=workspace.mannings_n_text_file,
        reanalysis_flow_file=workspace.DEM_Reanalsyis_FlowFile,
        river_id="COMID",
        highflow_field=configs.specified_highflow_field,
        baseflow_field=configs.specified_bathyflow_field,
        bathy_args=configs.bathy_args,
        include_baseflow=not configs.disable_bathymetry,
    )
    
    params["# VDT_Output_File_and_CurveFile"] = ""
    params["VDT_Database_NumIterations"] = configs.bathy_args.get("VDT_Database_NumIterations", DEFAULT_BATHY_ARGS["VDT_Database_NumIterations"])
    if configs.make_vdt:
        params["Print_VDT_Database"] = workspace.VDT_File_Bathy
    if configs.make_curvefile:
        params["Print_Curve_File"] = workspace.Curve_File_Bathy
    if configs.make_ap_database:
        params["Print_AP_Database"] = workspace.AP_File
    if configs.make_cross_section_file:
        params["XS_Out_File"] = workspace.Cross_Section_File

    params["reach_id"] = configs.stream_id_field
    params["downstream_reach_id"] = configs.downstream_id_field
    params["Reach_Average_Curve_File"] = configs.create_reach_average_curve_file
    params["StrmShp_File"] = stream_vector
    params["Slope_Low_Percentile"] = configs.slope_low_percentile
    params["Slope_High_Percentile"] = configs.slope_high_percentile

    if not configs.disable_bathymetry:
        if configs.use_power_laws_for_bathymetry:
            params["drainage_area_field"] = configs.area_km2_field
            params["coefficient_depth"] = configs.coefficient_depth or DEFAULT_CONFIG["coefficient_depth"]
            params["exponent_depth"] = configs.exponent_depth or DEFAULT_CONFIG["exponent_depth"]
            params["coefficient_width"] = configs.coefficient_width or DEFAULT_CONFIG["coefficient_width"]
            params["exponent_width"] = configs.exponent_width or DEFAULT_CONFIG["exponent_width"]

        params["# Mapper Input Data"] = ""
        params["Comid_Flow_File"] = workspace.COMID_Q_File
        # Use kernel method for bathymetry creation
        params["mapper"] = configs.mapper if not configs.mapper.is_curve2flood_fldpln_mapper() else Mapper.CURVE2FLOOD_KERNEL_WEIGHTED
        params["FS_ADJUST_FLOW_BY_FRACTION"] = configs.bathy_args.get("FS_ADJUST_FLOW_BY_FRACTION", DEFAULT_BATHY_ARGS["FS_ADJUST_FLOW_BY_FRACTION"])
        params["TW_MultFact"] = configs.bathy_args.get("TW_MultFact", DEFAULT_FLOODMAP_ARGS["TW_MultFact"])
        params["TopWidthPlausibleLimit"] = configs.bathy_args.get("TopWidthPlausibleLimit", DEFAULT_FLOODMAP_ARGS["TopWidthPlausibleLimit"])
        if not configs.bathy_args.get("Make_Output_GPKG", DEFAULT_FLOODMAP_ARGS["Make_Output_GPKG"]):
            params["Make_Output_GPKG"] = False

        if configs.use_specified_depth_for_bathy_mask:
            params["FloodSpreader_SpecifyDepth"] = configs.specify_depths_for_bathy_mask[-1]
        else:
            if not workspace.bathy_water_mask:
                raise ValueError("Bathy water mask is required when not using specified depth for bathy mask.")
            params["BathyWaterMask"] = workspace.bathy_water_mask

        params["# Bathymetry_Information"] = ""
        params["Bathy_Trap_H"] = configs.bathy_args.get("Bathy_Trap_H", DEFAULT_BATHY_ARGS["Bathy_Trap_H"])
        params["Bathy_Use_Banks"] = configs.bathy_use_banks

        if configs.flood_waterlc_and_strm_cells:
            params["Flood_WaterLC_and_STRM_Cells"] = True
            params["LAND_WaterValue"] = configs.land_watervalue
        if configs.find_banks_based_on_landcover:
            params["FindBanksBasedOnLandCover"] = True

        workspace.bathy_file_folder.mkdir(parents=True, exist_ok=True)
        params["BATHY_Out_File"] = workspace.ARC_BathyFile
        params["AROutBATHY"] = workspace.ARC_BathyFile
        params["FSOutBATHY"] = workspace.FS_BathyFile
    
    LOG.info(f"Writing ARC config to {workspace.ARC_FileName_Bathy}.")
    _write_config(workspace.ARC_FileName_Bathy, params)
    
    return workspace.ARC_FileName_Bathy

def define_mapper_configs(workspace: Workspace, flow_file: Path) -> Path:
    workspace.flood_folder.mkdir(parents=True, exist_ok=True)
    
    configs = workspace.configs
    params = {'#ARC_Inputs': ''}
    if configs.disable_bathymetry:
        if configs.clean_dem:
            params["DEM_File"] = workspace.DEM_File_Clean
        elif configs.burn_streams:
            params["DEM_File"] = workspace.fixed_dem
        else:
            params["DEM_File"] = workspace.assigned_dem
    else:
        params["DEM_File"] = workspace.FS_BathyFile
    params["Stream_File"] = workspace.new_stream_raster if configs.move_stream_network_to_thalweg else workspace.STRM_File_Clean
    params["LU_Manning_n"] = workspace.mannings_n_text_file

    params["# VDT_Output_File_and_CurveFile"] = ""
    
    params["Print_VDT_Database"] = workspace.VDT_File_Bathy
    if configs.make_curvefile:
        params["Print_Curve_File"] = workspace.Curve_File_Bathy
    
    params["# Mapper Input Data"] = ""
    params["StrmShp_File"] = workspace.new_StrmShp_matched if configs.move_stream_network_to_thalweg else workspace.DEM_StrmShp
    params["Comid_Flow_File"] = flow_file
    params["mapper"] = configs.mapper
    if configs.mapper.is_curve2flood_fldpln_mapper():
        params.update(_fldpln_inputs(workspace))
    params["FS_ADJUST_FLOW_BY_FRACTION"] = configs.floodmap_args.get("FS_ADJUST_FLOW_BY_FRACTION", DEFAULT_FLOODMAP_ARGS["FS_ADJUST_FLOW_BY_FRACTION"])
    params["TW_MultFact"] = configs.floodmap_args.get("TW_MultFact", DEFAULT_FLOODMAP_ARGS["TW_MultFact"])
    params["TopWidthPlausibleLimit"] = configs.floodmap_args.get("TopWidthPlausibleLimit", DEFAULT_FLOODMAP_ARGS["TopWidthPlausibleLimit"])

    if configs.flood_waterlc_and_strm_cells or configs.make_velocity_maps:
        params["Flood_WaterLC_and_STRM_Cells"] = configs.flood_waterlc_and_strm_cells
        params["LU_Raster_SameRes"] = workspace.LAND_File
        params["LAND_WaterValue"] = configs.land_watervalue

    if configs.floodmap_mode == FloodMapMode.FORECAST:
        if configs.streamflow_source.is_nwm():
            # create the end of the file name that describes the forecast
            if configs.forensic_forecast_date != None:
                ending_of_forecast_file = f'Forecast_{configs.forensic_forecast_date}_{configs.forensic_forecast_hour}'
            elif configs.forecastdate != None:
                ending_of_forecast_file = f'Forecast_{configs.forecastdate}_{configs.forecasthour}'
            # rename the forecast of the extent raster based upon the type of NWM forecast we are using
            postfix = f"_{configs.streamflow_source}_ARC_Flood{configs.floodmap_id}{ending_of_forecast_file}"
            config_path = workspace.ARC_Folder / f"{configs.streamflow_source}_ARC_Input_{workspace.FileName}_FloodForecast.{'yaml' if configs.use_yaml else 'txt'}"
        elif configs.streamflow_source == StreamflowSource.GEOGLOWS:
            # create the end of the file name that describes the forecast
            if configs.forensic_forecast_date != None:
                ending_of_forecast_file = f'Forecast_{configs.forensic_forecast_date}'
            elif configs.forecastdate != None:
                ending_of_forecast_file = f'Forecast_{configs.forecastdate}'
            postfix = ending_of_forecast_file
            config_path = workspace.ARC_Folder / f"{configs.streamflow_source}_ARC_Input_{workspace.FileName}_FloodForecast.{'yaml' if configs.use_yaml else 'txt'}"
    elif configs.floodmap_mode == FloodMapMode.USER:
        postfix = f"_{flow_file.stem}"
        if not flow_file.exists():
            LOG.error(f"User provided flow file does not exist: {flow_file}")
            raise FileNotFoundError(f"User provided flow file does not exist: {flow_file}")
        config_path = workspace.ARC_Folder / f"{configs.streamflow_source}_ARC_Input_{workspace.FileName}{configs.floodmap_id}{postfix}.{'yaml' if configs.use_yaml else 'txt'}"
    elif configs.floodmap_mode == FloodMapMode.RETURN_PERIOD:
        postfix = f"_{flow_file.stem.rsplit('_')[-1]}"
        config_path = workspace.ARC_Folder / f"{configs.streamflow_source}_ARC_Input_{workspace.FileName}{postfix}.{'yaml' if configs.use_yaml else 'txt'}"
    else:
        LOG.error(f"Invalid floodmap_mode: {configs.floodmap_mode}")
        raise ValueError(f"Invalid floodmap_mode: {configs.floodmap_mode}")

    floodmap_path = workspace.flood_folder / f"{configs.streamflow_source}_{workspace.FileName}_ARC_Flood{configs.floodmap_id}{postfix}.tif"
    params["OutFLD"] = floodmap_path

    if configs.floodmap_args.get('Make_Output_GPKG', DEFAULT_FLOODMAP_ARGS["Make_Output_GPKG"]):
        floodmap_vector = workspace.flood_folder / f"{configs.streamflow_source}_{workspace.FileName}_ARC_Flood{configs.floodmap_id}{postfix}.gpkg"
        params["OutSHP"] = floodmap_vector
    else:
        params["Make_Output_GPKG"] = False

    if configs.make_velocity_maps:
        velocity_path = workspace.flood_folder / f"{configs.streamflow_source}_{workspace.FileName}_ARC_FloodVEL{configs.floodmap_id}{postfix}.tif"
        params["OutVEL"] = velocity_path

    if configs.make_wse_maps:
        wse_path = workspace.flood_folder / f"{configs.streamflow_source}_{workspace.FileName}_ARC_FloodWSE{configs.floodmap_id}{postfix}.tif"
        params["OutWSE"] = wse_path

    if configs.make_depth_maps:
        depth_path = workspace.flood_folder / f"{configs.streamflow_source}_{workspace.FileName}_ARC_FloodDepth{configs.floodmap_id}{postfix}.tif"
        params["OutDEP"] = depth_path

    LOG.info(f"Writing Mapper config to {config_path}.")
    _write_config(config_path, params)
    return config_path
