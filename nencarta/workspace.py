from pathlib import Path

from nencarta.core.configs import NencartaConfig
from nencarta.core.enumerations import Mapper

# The folders a workspace writes to, which 'folder_paths' can move, and where they go by default
WORKSPACE_FOLDERS = ('DEM', 'ARC_InputFiles', 'FloodMap', 'Bathymetry', 'DEM_Updated', 'STRM', 'LAND',
                     'FLOW', 'VDT', 'FIST', 'Consequences', 'FlowDirection', 'FLDPLN')
DEFAULT_FOLDER_PATH = '{name}/{folder}'

# What each kind of file is called after its '<source>_<DEM>_' or '<DEM>_' prefix, which 'file_names' can change
FILE_BLURBS = (
    'dem', 'fixed', 'filled', 'fixed_Clean', 'Clean',
    'flowdir',
    'StrmShp', 'StrmShp_relocated', 'STRM_Raster_Clean', 'matched', 'lakes', 'stream_info', 'fldpln_library',
    'LAND_Raster', 'AR_Manning_n_MED',
    'Reanalysis', '2yr_flow_initial', 'rp', 'forecast', 'Flow_COMID_Q',
    'ARC_Input_Bathy', 'ARC_Input_InitialFlood', 'ARC_Input_FloodForecast', 'ARC_Input',
    'VDT_Database_Initial', 'VDT_Database_Bathy', 'AP_Database_Bathy', 'CurveFile_Initial',
    'CurveFile_Bathy', 'XS', 'Representative_XS',
    'Mapper_Input_Bathy', 'Mapper_Input_InitialFlood', 'Mapper_Input_DepthMask',
    'water_mask', 'depth_mask', 'ARC_Bathy', 'FS_Bathy',
    'Reanalysis_Boundary', 'ARC_Input_Boundary', 'VDT_Database_Boundary', 'ARC_Bathy_Boundary', 'outlet_wse',
    'ARC_Flood', 'ARC_Flood_Initial', 'ARC_FloodDepth', 'ARC_FloodWSE', 'ARC_FloodVEL',
    'Seed', 'FIST',
)

def check_workspaces_do_not_share_folders(workspaces: list['Workspace']) -> None:
    """
    Raise if two workspaces with short_file_names would write to the same folder: without the DEM in
    their file names, their files would overwrite each other.
    """
    owners = {}
    for workspace in workspaces:
        if not workspace.configs.short_file_names:
            continue
        for folder in WORKSPACE_FOLDERS:
            path = workspace.folder_path(folder)
            other = owners.setdefault(path, workspace)
            if other is not workspace:
                raise ValueError(
                    f"With 'short_file_names', {other.FileName} and {workspace.FileName} would both write to {path}. "
                    "Put {dem} in 'folder_paths', e.g. {\"default\": \"{name}/{dem}/{folder}\"}, so each DEM has its own folders."
                )

class Workspace:
    """The input and output paths for mapping one DEM of a watershed."""

    def __init__(self, configs: NencartaConfig, dem: Path | None = None):
        self.configs = configs
        self.watershed: str = configs.watershed_name
        self.output_dir = Path(configs.output_dir) / self.watershed
        self.mapper: Mapper = configs.mapper

        unknown_folders = set(configs.folder_paths or {}) - set(WORKSPACE_FOLDERS) - {'default'}
        if unknown_folders:
            raise ValueError(
                f"Watershed '{self.watershed}': 'folder_paths' has unknown folder(s) {sorted(unknown_folders)}. "
                f"Use 'default' or any of {list(WORKSPACE_FOLDERS)}."
            )

        file_names = configs.file_names or {}
        unknown_blurbs = set(file_names) - set(FILE_BLURBS)
        if unknown_blurbs:
            raise ValueError(
                f"Watershed '{self.watershed}': 'file_names' has unknown file(s) {sorted(unknown_blurbs)}. "
                f"Use any of {list(FILE_BLURBS)}."
            )
        bad_names = {blurb: name for blurb, name in file_names.items() if not isinstance(name, str) or any(sep in name for sep in '/\\')}
        if bad_names:
            raise ValueError(f"Watershed '{self.watershed}': 'file_names' must give each file a name with no '/' or '\\': {bad_names}.")

        self.strm_source = 'NWM' if configs.streamflow_source.is_nwm() else configs.streamflow_source.file_prefix
        self.setup_dem_name(dem)
        self.DEM_folder = self.folder_path('DEM')
        self.ARC_Folder = self.folder_path('ARC_InputFiles')
        self.flood_folder = self.folder_path('FloodMap')
        self.bathy_file_folder = self.folder_path('Bathymetry')
        self.dem_updated_folder = self.folder_path('DEM_Updated')
        self.strm_folder = self.folder_path('STRM')
        self.land_folder = self.folder_path('LAND')
        self.FLOW_Folder = self.folder_path('FLOW')
        self.VDT_Folder = self.folder_path('VDT')
        self.FIST_Folder = self.folder_path('FIST')
        self.Consequences_Folder = self.folder_path('Consequences')
        self.Flow_Direction_Folder = self.folder_path('FlowDirection')
        self.FLDPLN_Folder = self.folder_path('FLDPLN')
        
        configs_mannings_n = configs.mannings_text_file
        if configs_mannings_n:
            configs_mannings_n_path = Path(configs_mannings_n)
            if not configs_mannings_n_path.is_file():
                raise FileNotFoundError(f"Provided Manning's n text file not found: {configs_mannings_n}")
            self.mannings_n_text_file = configs_mannings_n_path
        else:
            self.mannings_n_text_file = self.file_path(self.land_folder, 'AR_Manning_n_MED', 'txt', None)

        self.floodmap_mode = configs.floodmap_mode

        self.setup_dem(dem)
        dem_updated, flow_direction = self.dem_updated_folder, self.Flow_Direction_Folder
        self.fixed_dem = self.file_path(dem_updated, 'fixed', 'tif')
        self.filled_dem = self.file_path(dem_updated, 'filled', 'tif')
        self.flowdir = self.file_path(flow_direction, 'flowdir', 'tif')
        stream_output_ext = "parquet" if configs.streams_as_parquet else "gpkg"
        stream_info_ext = "parquet" if configs.use_parquet else "csv"
        self.new_StrmShp_matched = self.file_path(self.strm_folder, 'matched', stream_output_ext)
        self.stream_info_file = self.file_path(self.FLDPLN_Folder, 'stream_info', stream_info_ext)
        self.new_stream_raster = self.file_path(self.strm_folder, 'matched', 'tif')
        self.lake_raster = self.file_path(self.strm_folder, 'lakes', 'tif')
        # Whether lake_raster was rasterized by this workspace, so that even with overwrite it is read back, not made again
        self.lake_raster_made = False
        # The ocean on a grid, as move_streams.load_ocean_array made it for this workspace: (grid, mask or None)
        self.ocean_array = None

        # currently the land file will be the same regardless of the streamflow source
        self.LAND_File = self.file_path(self.land_folder, 'LAND_Raster', 'tif')

        #Datasets to be Created
        source = configs.streamflow_source.file_prefix
        self.DEM_StrmShp = self.file_path(self.strm_folder, 'StrmShp', stream_output_ext, 'source',
                                          legacy=f"{source}_{self.FileName}_StrmShp.{stream_output_ext}")
        self.relocated_StrmShp = self.file_path(self.strm_folder, 'StrmShp_relocated', stream_output_ext, 'source')
        self.DEM_Reanalsyis_FlowFile = self.file_path(self.FLOW_Folder, 'Reanalysis', 'csv', 'source',
                                                      legacy=f"{source}_{self.FileName}_Reanalysis.csv")
        self.COMID_Q_File = self.file_path(self.FLOW_Folder, '2yr_flow_initial', 'csv')

        strm_source = self.strm_source
        self.ARC_FileName_Bathy = self.file_path(self.ARC_Folder, 'ARC_Input_Bathy', self.config_end, 'source',
                                                 legacy=f"{strm_source}_ARC_Input_{self.FileName}_Bathy.{self.config_end}")
        self.ARC_FileName_for_DEM_Cleaner = self.file_path(self.ARC_Folder, 'ARC_Input_InitialFlood', 'txt', 'source',
                                                           legacy=f"{strm_source}_ARC_Input_{self.FileName}_InitialFlood.txt")
        if configs.burn_streams:
            self.DEM_File_Clean = self.file_path(dem_updated, 'fixed_Clean', 'tif')
        else:
            self.DEM_File_Clean = self.file_path(dem_updated, 'Clean', 'tif')
        self.STRM_File_Clean = self.file_path(self.strm_folder, 'STRM_Raster_Clean', 'tif', 'source')

        vdt_ext = configs.vdt_file_extension
        self.VDT_File_Initial = self.file_path(self.VDT_Folder, 'VDT_Database_Initial', vdt_ext, 'source')
        self.VDT_File_Bathy = self.file_path(self.VDT_Folder, 'VDT_Database_Bathy', vdt_ext, 'source')

        self.AP_File = self.file_path(self.VDT_Folder, 'AP_Database_Bathy', vdt_ext, 'source')

        self.Curve_File_Initial = self.file_path(self.VDT_Folder, 'CurveFile_Initial', 'csv', 'source')
        self.Curve_File_Bathy = self.file_path(self.VDT_Folder, 'CurveFile_Bathy', 'csv', 'source')

        self.Cross_Section_File = self.file_path(self.VDT_Folder, 'XS', 'txt', 'source')
        self.Representative_Cross_Section_File = self.file_path(self.VDT_Folder, 'Representative_XS',
                                                                'parquet' if vdt_ext == 'parquet' else 'csv', 'source')

        self.bathy_water_mask = self.file_path(self.bathy_file_folder, 'water_mask', 'tif', 'source')
        # use_specified_depth_for_bathy_mask: ARC's water mask, the streams flooded to a set depth, and the Curve2Flood
        # config that makes it
        self.depth_mask = self.file_path(self.bathy_file_folder, 'depth_mask', 'tif', 'source')
        self.Depth_Mask_Config = self.file_path(self.ARC_Folder, 'Mapper_Input_DepthMask', self.config_end, 'source')
        # Curve2Flood's own configs beside ARC's: the run that burns ARC's bathymetry into the DEM (FS_Bathy), and the
        # DEM cleaner's outline with use_specified_depth_for_bathy_mask (FloodMapFile_Initial)
        self.Mapper_FileName_Bathy = self.file_path(self.ARC_Folder, 'Mapper_Input_Bathy', self.config_end, 'source')
        self.Mapper_FileName_InitialFlood = self.file_path(self.ARC_Folder, 'Mapper_Input_InitialFlood',
                                                           self.config_end, 'source')
        self.ARC_BathyFile = self.file_path(self.bathy_file_folder, 'ARC_Bathy', 'tif', 'source')
        self.FS_BathyFile = self.file_path(self.bathy_file_folder, 'FS_Bathy', 'tif', 'source')

        # outlet_exchange (see tasks/outlet_exchange.py): the folder the DEMs share, where pass1 writes this DEM's
        # water for its neighbours; pass1's cut-down ARC run; and pass2's outlet water, gathered from the neighbours'
        self.outlet_exchange_folder = Path(configs.outlet_exchange_dir) if configs.outlet_exchange_dir else             Path(configs.output_dir) / 'outlet_exchange'
        self.boundary_wse_file = self.outlet_exchange_folder / f"{self.dem_name}_boundary_wse.parquet"
        self.boundary_flow_file = self.file_path(self.FLOW_Folder, 'Reanalysis_Boundary', 'csv', 'source')
        self.ARC_FileName_Boundary = self.file_path(self.ARC_Folder, 'ARC_Input_Boundary', self.config_end, 'source')
        self.VDT_File_Boundary = self.file_path(self.VDT_Folder, 'VDT_Database_Boundary', 'parquet', 'source')
        self.ARC_BathyFile_Boundary = self.file_path(self.bathy_file_folder, 'ARC_Bathy_Boundary', 'tif', 'source')
        self.outlet_wse_file = self.file_path(self.ARC_Folder, 'outlet_wse', 'parquet', 'source')

        self.floodmap_id = configs.floodmap_id

        # The DEM cleaner's initial flood map
        flood_map = f"{strm_source}_{self.FileName}_ARC_Flood{self.floodmap_id}"
        self.FloodMapFile_Initial = self.file_path(self.flood_folder, 'ARC_Flood_Initial', 'tif', 'source', suffix=self.floodmap_id,
                                                   legacy=f"{flood_map}_Initial.tif")
        self.FloodMapFile_Initial_SHP = self.file_path(self.flood_folder, 'ARC_Flood_Initial', 'shp', 'source', suffix=self.floodmap_id,
                                                       legacy=f"{flood_map}_Initial.shp")

        if configs.mapper == Mapper.CURVE2FLOOD_FLDPLNPY:
            self.setup_fldpln_files()

        if configs.file_names or configs.short_file_names:
            self.check_file_paths_are_unique()

    def check_file_paths_are_unique(self):
        """
        Raise if file_names or short_file_names give two files the same path. Only the DEM may have two names:
        without a buffer or bbox, the DEM nencarta works on is the input DEM itself.
        """
        owners = {}
        for attr, path in vars(self).items():
            if not isinstance(path, Path) or attr == 'output_dir' or attr.lower().endswith('_folder'):
                continue
            other = owners.setdefault(path, attr)
            if other != attr and {other, attr} != {'original_dem', 'assigned_dem'}:
                raise ValueError(f"Watershed '{self.watershed}': 'file_names' gives {other} and {attr} the same path, {path}.")

    @property
    def model_StrmShp(self) -> Path:
        """
        The stream network the models use: the moved network when moving streams, otherwise the source streams.
        Use this rather than DEM_StrmShp after the move, since minimize_output_files deletes DEM_StrmShp.
        """
        if self.configs.move_stream_network_to_thalweg:
            return self.new_StrmShp_matched
        return self.DEM_StrmShp

    @property
    def model_dem(self) -> Path:
        """The DEM ARC runs on: the cleaned DEM with clean_dem, else the burned DEM with burn_streams, else the DEM."""
        if self.configs.clean_dem:
            return self.DEM_File_Clean
        if self.configs.burn_streams:
            return self.fixed_dem
        return self.assigned_dem

    @property
    def config_end(self) -> str:
        """The extension of the ARC and Curve2Flood input files."""
        return 'yaml' if self.configs.use_yaml else 'txt'

    def setup_dem_name(self, dem: Path | None):
        """Set the DEM's name, and the name its output files start with (FileName)."""
        if dem:
            self.dem_name = Path(dem).stem
        elif self.configs.bbox:
            # the 5th decimal place corresponds to ~1m resolution, which is more precise than we need for naming purposes, so we can round to 5 decimal places for cleaner file names
            self.dem_name = f"dem_{self.configs.bbox[0]:.5f}_{self.configs.bbox[1]:.5f}_{self.configs.bbox[2]:.5f}_{self.configs.bbox[3]:.5f}"
        else:
            raise ValueError("Must specify either a DEM or a bounding box in the configs.")
        self.FileName = self.dem_name

        if self.configs.buffer:
            if not self.configs.source_dems:
                raise ValueError(
                    "Buffering requested but no source DEMs assigned."
                )
            self.FileName += '_buffered'

    def folder_path(self, folder: str) -> Path:
        """
        Where ``folder`` (one of WORKSPACE_FOLDERS) goes: its template in folder_paths, else the 'default' one,
        else '{name}/{folder}'. Relative paths are inside output_dir.
        """
        folder_paths = self.configs.folder_paths or {}
        template = str(folder_paths.get(folder, folder_paths.get('default', DEFAULT_FOLDER_PATH)))
        try:
            path = Path(template.format(output_dir=Path(self.configs.output_dir).absolute(), name=self.watershed,
                                        dem=self.dem_name, folder=folder))
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"Watershed '{self.watershed}': 'folder_paths' template {template!r} for {folder} is invalid ({exc!r}). "
                "Templates can use {output_dir}, {name}, {dem} and {folder}."
            ) from exc
        return path if path.is_absolute() else Path(self.configs.output_dir) / path

    def file_path(self, folder: Path, blurb: str, ext: str, prefix: str | None = 'dem', suffix: str = '',
                  legacy: str | None = None) -> Path:
        """
        A file in ``folder`` named '<prefix><blurb><suffix>.<ext>'.

        ``blurb`` (one of FILE_BLURBS) says what the file is, and is the name file_names replaces; an empty
        replacement leaves just the suffix. ``suffix`` tells files of one kind apart, e.g. '_rp100'. ``prefix``
        is 'source' for '<source>_<FileName>_', 'dem' for '<FileName>_', or None; short_file_names drops it.
        ``legacy`` is the usual name of a file that doesn't follow this pattern, used unless file_names or
        short_file_names changes it.
        """
        renamed = (self.configs.file_names or {}).get(blurb)
        if legacy is not None and renamed is None and not self.configs.short_file_names:
            return folder / legacy
        name = suffix.lstrip('_') if renamed == '' and suffix.strip('_') else f"{renamed or blurb}{suffix}"
        if not self.configs.short_file_names:
            name = {'source': f"{self.strm_source}_{self.FileName}_", 'dem': f"{self.FileName}_", None: ''}[prefix] + name
        return folder / f"{name}.{ext}"

    def return_period_flow_file(self, return_period: int) -> Path:
        """The flow file for one return period."""
        return self.file_path(self.FLOW_Folder, 'rp', 'csv', suffix=str(return_period))

    def setup_dem(self, dem: Path | None):
        """Set the DEM nencarta works on: the input DEM, or one cut from source DEMs with bbox or buffer."""
        self.original_dem = dem
        if dem and not self.configs.bbox and not self.configs.buffer:
            self.assigned_dem = Path(dem)
        else:
            ext = 'vrt' if self.configs.use_vrt else 'tif'
            self.assigned_dem = self.file_path(self.DEM_folder, 'dem', ext, legacy=f"{self.FileName}.{ext}")

    def setup_fldpln_files(self):
        """Add the FLDPLN library's path."""
        self.fldpln_library = self.file_path(self.FLDPLN_Folder, 'fldpln_library', 'parquet', None)

    def __repr__(self):
        return f"Workspace(watershed={self.watershed}, output_dir={self.output_dir})"
