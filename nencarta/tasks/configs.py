import io
from pathlib import Path

import pyarrow.parquet as pq
import pyogrio
import yaml

from nencarta.core.configs import NencartaConfig
from nencarta.core.enumerations import FloodMapMode, Mapper
from nencarta.core.model_args import get_arg, merge_args
from nencarta.logger import LOG
from nencarta.workspace import Workspace

# ARC's drainage-area power law keys, which nencarta gives their field (see _drainage_area_power_laws)
_POWER_LAW_KEYS = ("coefficient_width", "exponent_width", "coefficient_depth", "exponent_depth")


def _arc_inputs(dem: Path,
                stream_raster: Path,
                land_cover: Path,
                mannings_n_file: Path,
                reanalysis_flow_file: Path,
                river_id: str,
                highflow_field: str,
                baseflow_field: str,
                land_watervalue: int,
                include_baseflow: bool = True,
                ):
    """The input section of an ARC config."""
    params = {
        "#ARC_Inputs": "",
        "DEM_File": dem,
        "Stream_File": stream_raster,
        "LU_Raster_SameRes": land_cover,
        "LU_Manning_n": mannings_n_file,
        # The land cover's water, for the banks with FindBanksBasedOnLandCover
        "LC_Water_Value": land_watervalue,
        "Flow_File": reanalysis_flow_file,
        "Flow_File_ID": river_id,
    }
    if include_baseflow:
        params["Flow_File_BF"] = baseflow_field
    params["Flow_File_QMax"] = highflow_field

    return params


def _with_arc_args(params: dict, configs: NencartaConfig, stream_vector: Path, power_laws: bool = True) -> dict:
    """An ARC config's ``params`` with arc_args after them, and their power laws given their drainage area field (see
    _drainage_area_power_laws), or left out without ``power_laws``."""
    params = merge_args({**params, "# arc_args": ""}, configs.arc_args)
    laws = _drainage_area_power_laws(configs, stream_vector) if power_laws else \
        dict.fromkeys(_POWER_LAW_KEYS)
    return merge_args(params, laws)

def _vector_columns(path: Path) -> set[str] | None:
    """The attribute columns of a stream file, or None if it isn't there yet."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        if path.suffix.lower() == ".parquet":
            return set(pq.read_schema(path).names)
        return set(pyogrio.read_info(path)["fields"])
    except Exception:  # not a stream file yet, such as an empty placeholder: as if it weren't there  # noqa: BLE001
        return None


def _drainage_area_power_laws(configs: NencartaConfig, stream_vector: Path) -> dict:
    """
    arc_args' drainage-area power laws, given the field of the area in km2: the width's (nencarta's by default), and
    the depth's when arc_args give coefficient_depth and exponent_depth. ARC sizes with the width's the channels whose
    banks the water mask, the land cover and the DEM don't show: a single cell as wide as the law says, where that's
    narrower than a cell. Without it, on a flat valley floor, ARC found banks at a few of a creek's cross sections,
    hundreds of metres to kilometres apart.

    The area is area_km2_field's, which nencarta adds to the stream lines (tasks.make_stream_geometry). A stream file
    made before it did has only area_m2_field: the coefficients are then scaled to take m2, which gives the same
    widths and depths. Without either field, ARC gets no power laws: their keys are None, to drop them.
    """
    laws = {key: get_arg(configs.arc_args, key) for key in _POWER_LAW_KEYS}
    laws = {key: value for key, value in laws.items() if value is not None}
    if not laws:
        return {}

    field, scale = configs.area_km2_field, 1.0
    columns = _vector_columns(stream_vector)
    if columns is not None and field not in columns:
        if configs.area_m2_field and configs.area_m2_field in columns:
            field, scale = configs.area_m2_field, 1e-6
        else:
            LOG.warning(f"The stream lines {stream_vector} have no drainage area ({configs.area_km2_field} or "
                        f"{configs.area_m2_field}), so ARC gets no power laws.")
            return dict.fromkeys(_POWER_LAW_KEYS)

    params = {"drainage_area_field": field}
    for kind in ("width", "depth"):
        coefficient, exponent = laws.get(f"coefficient_{kind}"), laws.get(f"exponent_{kind}")
        if coefficient is not None and exponent is not None:
            params[f"coefficient_{kind}"] = float(coefficient) * scale ** float(exponent)
            params[f"exponent_{kind}"] = float(exponent)

    return params


def _fldpln_inputs(workspace: Workspace):
    """The FLDPLN section of a Curve2Flood config. The mapper reads neither the filled DEM (the VDT's Elev and the
    library's fill give it at the stream) nor the stream network."""
    output = {
        "# FLDPLN_Inputs": "",
        "Flow_Direction_File": workspace.flowdir,
        "Stream_Info_File": workspace.stream_info_file,
        "FLDPLN_Library": workspace.fldpln_library,
    }
    output[""] = ""
    return output

def _write_config(config_path: Path, params: dict):
    """
    Write an ARC or Curve2Flood config: YAML for a .yaml path, otherwise tab-separated text. Keys starting with
    '#' are section headers.
    """
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
    """Whether every output ARC is asked for (VDT, curve file, AP database, representative cross sections) exists."""
    configs = workspace.configs
    asked_for = (
        (workspace.VDT_File_Bathy, configs.make_vdt),
        (workspace.Curve_File_Bathy, configs.make_curvefile),
        (workspace.AP_File, configs.make_ap_database),
        (workspace.Representative_Cross_Section_File, configs.make_representative_cross_section_file),
    )
    return all(path.exists() for path, wanted in asked_for if wanted)

def forecast_postfix(configs: NencartaConfig, date: str, hour: str | None) -> str:
    """The end of the flood map names of the forecast issued on ``date`` (and at ``hour``, for NWM)."""
    if configs.streamflow_source.is_nwm():
        return f"_{configs.streamflow_source.file_prefix}_ARC_Flood{configs.floodmap_id}Forecast_{date}_{hour}"
    
    return f"Forecast_{date}"

def flood_map_paths(workspace: Workspace, postfix: str) -> dict[str, Path]:
    """The flood maps whose names end with ``postfix``, by the mapper config key that writes each."""
    configs = workspace.configs
    suffix = f"{configs.floodmap_id}{postfix}"

    def path(blurb: str, ext: str) -> Path:
        return workspace.file_path(workspace.flood_folder, blurb, ext, 'source', suffix=suffix,
                                   legacy=f"{configs.streamflow_source.file_prefix}_{workspace.FileName}_{blurb}{suffix}.{ext}")

    return {"OutFLD": path('ARC_Flood', 'tif'), "OutSHP": path('ARC_Flood', 'gpkg'), "OutVEL": path('ARC_FloodVEL', 'tif'),
            "OutWSE": path('ARC_FloodWSE', 'tif'), "OutDEP": path('ARC_FloodDepth', 'tif')}
                
def define_configs_for_dem_cleaning(workspace: Workspace) -> Path:
    """Write the config of the DEM cleaner's ARC run (initial rating curves): nencarta's files and fields, and
    arc_args. Its outline, with use_specified_depth_for_bathy_mask, is Curve2Flood's (see clean_dem)."""
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
        land_watervalue=configs.land_watervalue,
        include_baseflow=True,
    )
    params["StrmShp_File"] = streamlines
    params["reach_id"] = configs.stream_id_field
    params["downstream_reach_id"] = configs.downstream_id_field

    params["#VDT_Output_File_and_CurveFile"] = ""
    params["Print_VDT_Database"] = workspace.VDT_File_Initial
    params["Print_Curve_File"] = workspace.Curve_File_Initial

    _write_config(workspace.ARC_FileName_for_DEM_Cleaner, _with_arc_args(params, configs, streamlines))
    return workspace.ARC_FileName_for_DEM_Cleaner

def define_arc_configs(workspace: Workspace,) -> Path:
    """
    Write the config of ARC's main run (rating curves and bathymetry): nencarta's files and fields, and arc_args.
    Without bathymetry, nothing is rewritten if ARC's outputs exist.
    """
    workspace.ARC_Folder.mkdir(parents=True, exist_ok=True)
    workspace.VDT_Folder.mkdir(parents=True, exist_ok=True)
    workspace.flood_folder.mkdir(parents=True, exist_ok=True)

    configs = workspace.configs
    if configs.disable_bathymetry and _has_required_arc_outputs(workspace) and not configs.overwrite:
        LOG.info(f"Domain {workspace.watershed} has required ARC outputs and bathymetry is disabled, skipping ARC.")
        return workspace.ARC_FileName_Bathy

    if configs.move_stream_network_to_thalweg:
        stream_raster = workspace.new_stream_raster
        stream_vector = workspace.new_StrmShp_matched
    else:
        stream_raster = workspace.STRM_File_Clean
        stream_vector = workspace.DEM_StrmShp


    params = _arc_inputs(
        dem=workspace.model_dem,
        stream_raster=stream_raster,
        land_cover=workspace.LAND_File,
        mannings_n_file=workspace.mannings_n_text_file,
        reanalysis_flow_file=workspace.DEM_Reanalsyis_FlowFile,
        river_id="COMID",
        highflow_field=configs.specified_highflow_field,
        baseflow_field=configs.specified_bathyflow_field,
        land_watervalue=configs.land_watervalue,
        include_baseflow=not configs.disable_bathymetry,
    )

    params["# VDT_Output_File_and_CurveFile"] = ""
    if configs.make_vdt:
        params["Print_VDT_Database"] = workspace.VDT_File_Bathy
    if configs.make_curvefile:
        params["Print_Curve_File"] = workspace.Curve_File_Bathy
    if configs.make_ap_database:
        params["Print_AP_Database"] = workspace.AP_File
    if configs.make_cross_section_file:
        params["XS_Out_File"] = workspace.Cross_Section_File
    if configs.make_representative_cross_section_file:
        # ARC builds them in this run, from the cross sections it makes the rating curves from
        params["Build_Representative_Cross_Section"] = True
        params["Representative_Cross_Section_File"] = workspace.Representative_Cross_Section_File

    params["StrmShp_File"] = stream_vector
    if configs.standard_step or not configs.disable_bathymetry:
        # ARC links the cross sections down the stream network for the standard step and the bathymetry, so it needs
        # each reach's id and the one below it
        params["reach_id"] = configs.stream_id_field
        params["downstream_reach_id"] = configs.downstream_id_field

    if configs.outlet_exchange == "pass2" and workspace.outlet_wse_file.exists():
        # The neighbouring DEMs' water where the rivers leave this one, to start them from (tasks/outlet_exchange.py)
        params["Outlet_WSE_File"] = workspace.outlet_wse_file

    if not configs.disable_bathymetry:
        params["# Bathymetry_Information"] = ""
        # ARC puts each cross section's banks where its water mask's water ends: the streams flooded to a set depth
        # (make_depth_mask) with use_specified_depth_for_bathy_mask, otherwise the streams and the water at the
        # river's level beside them (make_water_mask)
        params["BathyWaterMask"] = workspace.depth_mask if configs.use_specified_depth_for_bathy_mask \
            else workspace.bathy_water_mask
        workspace.bathy_file_folder.mkdir(parents=True, exist_ok=True)
        params["BATHY_Out_File"] = workspace.ARC_BathyFile

    # ARC's power laws carve the bathymetry's channels
    params = _with_arc_args(params, configs, stream_vector, power_laws=not configs.disable_bathymetry)
    LOG.info(f"Writing ARC config to {workspace.ARC_FileName_Bathy}.")
    _write_config(workspace.ARC_FileName_Bathy, params)

    return workspace.ARC_FileName_Bathy


def define_mapper_bathymetry_config(workspace: Workspace) -> Path | None:
    """
    Write the config of the flood mapper's run that burns ARC's bathymetry into the DEM the flood maps are made on
    (FS_Bathy), as ARC estimated it (Bathy_Use_Banks says how). It maps no flood. None without bathymetry.
    """
    configs = workspace.configs
    if configs.disable_bathymetry:
        return None

    _write_config(workspace.Mapper_FileName_Bathy, {
        "# Curve2Flood_Inputs": "",
        "DEM_File": workspace.model_dem,
        "BATHY_Out_File": workspace.ARC_BathyFile,
        "Bathy_Use_Banks": configs.bathy_use_banks,
        "FSOutBATHY": workspace.FS_BathyFile,
    })
    return workspace.Mapper_FileName_Bathy

def define_mapper_configs(workspace: Workspace, flow_file: Path) -> Path:
    """
    Write the flood mapper's config for one flow file: a forecast, a user flow file or a return period. Its flood
    maps are named after the flows.
    """
    workspace.flood_folder.mkdir(parents=True, exist_ok=True)
    
    configs = workspace.configs
    config_ext = 'yaml' if configs.use_yaml else 'txt'
    # Curve2Flood reads the stream raster only with Set_Depth, Manning's n only for velocity, and the stream lines
    # only to allow the GeoPackage; FloodSpreader gets them all
    curve2flood = configs.mapper.is_curve2flood_mapper()
    params = {'#ARC_Inputs': ''}
    params["DEM_File"] = workspace.model_dem if configs.disable_bathymetry else workspace.FS_BathyFile
    if not curve2flood:
        params["Stream_File"] = workspace.new_stream_raster if configs.move_stream_network_to_thalweg else workspace.STRM_File_Clean
        
    if not curve2flood or configs.make_velocity_maps:
        params["LU_Manning_n"] = workspace.mannings_n_text_file

    params["# VDT_Output_File_and_CurveFile"] = ""

    # The mapper reads the VDT when it is given, and otherwise the curve file
    if configs.make_vdt:
        params["Print_VDT_Database"] = workspace.VDT_File_Bathy
    if configs.make_curvefile:
        params["Print_Curve_File"] = workspace.Curve_File_Bathy
    
    params["# Mapper Input Data"] = ""
    params["Comid_Flow_File"] = flow_file
    params["mapper"] = configs.mapper
    if configs.mapper.is_curve2flood_fldpln_mapper():
        params.update(_fldpln_inputs(workspace))

    # The files curve2flood_args' Flood_WaterLC_and_STRM_Cells (and the velocity) and Flood_Bathy_Mask need
    if configs.flood_waterlc_and_strm_cells or configs.make_velocity_maps:
        params["LU_Raster_SameRes"] = workspace.LAND_File
        params["LAND_WaterValue"] = configs.land_watervalue
        
    if configs.flood_bathy_mask:
        params["BathyWaterMask"] = workspace.bathy_water_mask

    if configs.floodmap_mode == FloodMapMode.FORECAST:
        if configs.forensic_forecast_date:
            postfix = forecast_postfix(configs, configs.forensic_forecast_date, configs.forensic_forecast_hour)
        else:
            postfix = forecast_postfix(configs, configs.forecastdate, configs.forecasthour)
        config_path = workspace.file_path(workspace.ARC_Folder, 'ARC_Input_FloodForecast', config_ext, 'source',
                                          legacy=f"{configs.streamflow_source.file_prefix}_ARC_Input_{workspace.FileName}_FloodForecast.{config_ext}")
    elif configs.floodmap_mode == FloodMapMode.USER:
        postfix = f"_{flow_file.stem}"
        if not flow_file.exists():
            LOG.error(f"User provided flow file does not exist: {flow_file}")
            raise FileNotFoundError(f"User provided flow file does not exist: {flow_file}")
        config_path = workspace.file_path(workspace.ARC_Folder, 'ARC_Input', config_ext, 'source', suffix=f"{configs.floodmap_id}{postfix}",
                                          legacy=f"{configs.streamflow_source.file_prefix}_ARC_Input_{workspace.FileName}{configs.floodmap_id}{postfix}.{config_ext}")
    elif configs.floodmap_mode == FloodMapMode.RETURN_PERIOD:
        return_period = next((rp for rp in configs.return_periods if workspace.return_period_flow_file(rp) == Path(flow_file)), None)
        postfix = f"_rp{return_period}" if return_period is not None else f"_{flow_file.stem.rsplit('_')[-1]}"
        config_path = workspace.file_path(workspace.ARC_Folder, 'ARC_Input', config_ext, 'source', suffix=postfix,
                                          legacy=f"{configs.streamflow_source.file_prefix}_ARC_Input_{workspace.FileName}{postfix}.{config_ext}")
    else:
        LOG.error(f"Invalid floodmap_mode: {configs.floodmap_mode}")
        raise ValueError(f"Invalid floodmap_mode: {configs.floodmap_mode}")

    flood_maps = flood_map_paths(workspace, postfix)
    params["OutFLD"] = flood_maps["OutFLD"]

    if configs.make_velocity_maps:
        params["OutVEL"] = flood_maps["OutVEL"]

    if configs.make_wse_maps:
        params["OutWSE"] = flood_maps["OutWSE"]

    if configs.make_depth_maps:
        params["OutDEP"] = flood_maps["OutDEP"]

    LOG.info(f"Writing Mapper config to {config_path}.")
    _write_config(config_path, merge_args({**params, "# curve2flood_args": ""}, configs.curve2flood_args))
    return config_path
