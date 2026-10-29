from pathlib import Path

from nencarta import dem_cleaner
from nencarta.logger import LOG
from nencarta.tasks.make_bathy_water_mask import flood_streams_to_depth
from nencarta.tasks.make_flow_file_from_reanalysis import (
    _make_flood_flow_file_from_base_max_file,
)
from nencarta.tasks.run_models import run_arc_for_initial_floodmap
from nencarta.workspace import Workspace


def make_clean_dem(workspace: Workspace) -> Path:
    """
    Smooth the DEM's stream channels with the DEM cleaner (workspace.DEM_File_Clean). ARC first estimates each
    reach's channel size. The cleaner outlines the channels with an initial flood map: the streams flooded to the
    first of specify_depths_for_bathy_mask with use_specified_depth_for_bathy_mask, and otherwise the water mask.
    """
    configs = workspace.configs
    if workspace.DEM_File_Clean.exists() and not configs.overwrite:
        return workspace.DEM_File_Clean

    if configs.overwrite:
        # Remake ARC's and the DEM cleaner's outputs, which the cleaner would otherwise reuse
        for path in (
            workspace.DEM_File_Clean,
            workspace.Curve_File_Initial,
            workspace.VDT_File_Initial,
        ):
            path.unlink(missing_ok=True)

        dem_name = workspace.DEM_File_Clean.name
        for intermediate_name in (
            f"Elev_Streams_{dem_name}",
            f"FLOOD_IMPACT_{dem_name}",
            f"SEED_CONNECT_{dem_name}",
        ):
            intermediate_path = workspace.dem_updated_folder / intermediate_name
            intermediate_path.unlink(missing_ok=True)
        
    if not workspace.Curve_File_Initial.exists():
        run_arc_for_initial_floodmap(workspace.ARC_FileName_for_DEM_Cleaner, configs)
        if not workspace.Curve_File_Initial.exists():
            raise FileNotFoundError(f"Expected Curve2Flood output file not found at {workspace.Curve_File_Initial} after running ARC for initial floodmap.")
    else:
        LOG.info(f"{workspace.Curve_File_Initial} exists and we aren't making it again...")

    if configs.use_specified_depth_for_bathy_mask:
        # On the DEM the cleaner's ARC run had
        dem = workspace.fixed_dem if configs.burn_streams else workspace.assigned_dem
        initial_flood_map = flood_streams_to_depth(workspace, dem, configs.specify_depths_for_bathy_mask[0],
                                                   workspace.FloodMapFile_Initial,
                                                   workspace.Mapper_FileName_InitialFlood)
        if not initial_flood_map.exists():
            raise FileNotFoundError(f"The flood mapper did not make the DEM cleaner's initial flood map, {initial_flood_map}.")
    else:
        # Water land cover and stream cells
        initial_flood_map = workspace.bathy_water_mask

    OutputID = 'COMID'
    Q_Fraction = 0.10
    TopWidthPlausibleLimit = 600
    search_dist_perp_cells = 10 # this was 40
    FlowFileName = workspace.file_path(workspace.FLOW_Folder, 'Flow_COMID_Q', 'txt')
    _make_flood_flow_file_from_base_max_file(
        reanalysis_flow_file=workspace.DEM_Reanalsyis_FlowFile,
        out_file=FlowFileName,
        columns=[OutputID, workspace.configs.specified_bathyflow_field],
    )

    workspace.dem_updated_folder.mkdir(parents=True, exist_ok=True)

    if workspace.configs.burn_streams:
        dem = workspace.fixed_dem
    else:
        dem = workspace.assigned_dem

    if workspace.configs.move_stream_network_to_thalweg:
        streamlines = workspace.new_StrmShp_matched
        stream_raster = workspace.new_stream_raster
    else:
        streamlines = workspace.DEM_StrmShp
        stream_raster = workspace.STRM_File_Clean

    dem_cleaner.DEM_Cleaner_Program(OutputID,
                                    streamlines, 
                                    dem.parent, 
                                    [dem.name], 
                                    [stream_raster], 
                                    workspace.dem_updated_folder, 
                                    FlowFileName, 
                                    workspace.Curve_File_Initial,
                                    initial_flood_map,
                                    Q_Fraction,
                                    TopWidthPlausibleLimit, 
                                    workspace.configs.compression, 
                                    search_dist_perp_cells)
