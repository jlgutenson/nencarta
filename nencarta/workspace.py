from pathlib import Path

from nencarta.core.enumerations import FloodMapMode, Mapper, StreamflowSource
from nencarta.core.configs import NencartaConfig

class Workspace:
    """Paths and per-DEM state for one watershed processing run."""

    def __init__(self, configs: NencartaConfig, dem: Path | None = None):
        self.configs = configs
        self.watershed: str = configs.watershed_name
        self.output_dir = Path(configs.output_dir) / self.watershed
        self.mapper: Mapper = configs.mapper

        self.DEM_folder = self.output_dir / 'DEM'
        self.ARC_Folder = self.output_dir / 'ARC_InputFiles'
        self.flood_folder = self.output_dir / 'FloodMap'
        self.bathy_file_folder = self.output_dir / 'Bathymetry'
        self.dem_updated_folder = self.output_dir / 'DEM_Updated'
        self.strm_folder = self.output_dir / 'STRM'
        self.land_folder = self.output_dir / 'LAND'
        self.FLOW_Folder = self.output_dir / 'FLOW'
        self.VDT_Folder = self.output_dir / 'VDT'
        self.XS_Folder = self.output_dir / 'XS'
        self.ESA_LC_Folder = self.output_dir / 'ESA_LC'
        self.FIST_Folder = self.output_dir / 'FIST'
        self.Consequences_Folder = self.output_dir / 'Consequences'
        self.Flow_Direction_Folder = self.output_dir / 'FlowDirection'

        configs_mannings_n = configs.mannings_text_file
        if configs_mannings_n:
            configs_mannings_n_path = Path(configs_mannings_n)
            if not configs_mannings_n_path.is_file():
                raise FileNotFoundError(f"Provided Manning's n text file not found: {configs_mannings_n}")
            self.mannings_n_text_file = configs_mannings_n_path
        else:
            self.mannings_n_text_file = self.land_folder / 'AR_Manning_n_MED.txt'

        self.floodmap_mode = configs.floodmap_mode

        self.setup_dem(dem)
        self.fixed_dem = self.dem_updated_folder / (self.FileName + '_fixed.tif')
        self.filled_dem = self.dem_updated_folder / (self.FileName + '_filled.tif') 
        self.flowdir = self.Flow_Direction_Folder / (self.FileName + '_flowdir.tif')
        self.flowacc = self.Flow_Direction_Folder / (self.FileName + '_flowacc.tif')
        self.new_StrmShp = self.Flow_Direction_Folder / (self.FileName + '_wtbx_derived.shp')
        self.whitebox_stream_raster = self.Flow_Direction_Folder / (self.FileName + '_wtbx_derived.tif')
        stream_output_ext = "parquet" if configs.streams_as_parquet else "gpkg"
        stream_info_ext = "parquet" if configs.use_parquet else "csv"
        self.new_StrmShp_matched = self.strm_folder / (self.FileName + f'_matched.{stream_output_ext}')
        self.stream_info_file = self.strm_folder / (self.FileName + f'_stream_info.{stream_info_ext}')
        self.new_stream_raster = self.strm_folder / (self.FileName + '_matched.tif')
        self.lake_raster = self.strm_folder / (self.FileName + '_lakes.tif')

        # currently the land file will be the same regardless of the streamflow source
        self.LAND_File = self.land_folder / (self.FileName + '_LAND_Raster.tif')

        #Datasets to be Created
        streamflow_source: StreamflowSource = configs.streamflow_source
        self.DEM_StrmShp = self.strm_folder / f"{streamflow_source}_{self.FileName}_StrmShp.{stream_output_ext}"
        self.DEM_Reanalsyis_FlowFile = self.FLOW_Folder / f"{streamflow_source}_{self.FileName}_Reanalysis.csv"
        self.COMID_Q_File = (self.DEM_Reanalsyis_FlowFile.parent / f"{self.FileName}_2yr_flow_initial.csv")

        # isolating the NWM or GEOGLOWS text in the streamflow_source variable
        strm_source = 'NWM' if streamflow_source.is_nwm() else 'GEOGLOWS'
        # these will only vary based upon if they are NWM or GEOGLOWS
        self.ARC_FileName_Bathy = self.ARC_Folder / f"{strm_source}_ARC_Input_{self.FileName}_Bathy.{self.config_end}"
        self.ARC_FileName_for_DEM_Cleaner = self.ARC_Folder / f"{strm_source}_ARC_Input_{self.FileName}_InitialFlood.txt"
        if configs.burn_streams:
            self.DEM_File_Clean = self.dem_updated_folder / f"{self.FileName}_fixed_Clean.tif"
        else:
            self.DEM_File_Clean = self.dem_updated_folder / f"{self.FileName}_Clean.tif"
        self.STRM_File = self.strm_folder / f"{strm_source}_{self.FileName}_STRM_Raster.tif"
        self.STRM_File_Clean = self.STRM_File.with_name(self.STRM_File.stem + '_Clean.tif')

        vdt_ext = configs.vdt_file_extension
        VDT_File = self.VDT_Folder / f"{strm_source}_{self.FileName}_VDT_Database.{vdt_ext}"
        self.VDT_File_Initial = VDT_File.with_name(VDT_File.stem + f"_Initial.{vdt_ext}")
        self.VDT_File_Bathy = VDT_File.with_name(VDT_File.stem + f"_Bathy.{vdt_ext}")

        self.AP_File =  self.VDT_Folder / f"{strm_source}_{self.FileName}_AP_Database_Bathy.{vdt_ext}"

        self.Curve_File = self.VDT_Folder / f"{strm_source}_{self.FileName}_CurveFile.csv"
        self.Curve_File_Initial = self.Curve_File.with_name(self.Curve_File.stem + '_Initial.csv')
        self.Curve_File_Bathy = self.Curve_File.with_name(self.Curve_File.stem + '_Bathy.csv')

        self.Cross_Section_File = self.VDT_Folder / f"{strm_source}_{self.FileName}_XS.txt"

        # ARC derives the companion coefficients path by appending _exponents.
        self.representative_cross_section_file = self.XS_Folder / f"{strm_source}_{self.FileName}_representative_cross_section.csv"
        self.representative_cross_section_coefficients_file = self.representative_cross_section_file.with_name(
            self.representative_cross_section_file.stem + '_exponents.csv'
        )

        # self.LU_and_Streams_Water_Map = self.flood_folder / f"{strm_source}_{self.FileName}_ARC_Flood_Initial.tif"
        self.bathy_water_mask = self.bathy_file_folder / f"{strm_source}_{self.FileName}_water_mask.tif"
        self.DepthMapFile = self.flood_folder / f"{strm_source}_{self.FileName}_ARC_Depth.tif"
        self.ARC_BathyFile = self.bathy_file_folder / f"{strm_source}_{self.FileName}_ARC_Bathy.tif"
        self.FS_BathyFile = self.bathy_file_folder / f"{strm_source}_{self.FileName}_FS_Bathy.tif"

        self.floodmap_id = configs.floodmap_identifier
        if self.floodmap_id:
            self.floodmap_id = f"_{self.floodmap_id}"
        else:
            self.floodmap_id = ''

        self.FloodMapFile = self.flood_folder / f"{strm_source}_{self.FileName}_ARC_Flood{self.floodmap_id}.tif"
        self.FloodMapFile_Initial = self.FloodMapFile.with_name(self.FloodMapFile.stem + '_Initial.tif')
        self.FloodMapFile_Initial_SHP = self.FloodMapFile.with_name(self.FloodMapFile.stem + '_Initial.shp')
        self.FloodMapFile_Bathy = self.FloodMapFile.with_name(self.FloodMapFile.stem + '_Bathy.tif')
        self.FloodMapFile_Bathy_SHP = self.FloodMapFile.with_name(self.FloodMapFile.stem + '_Bathy.shp')

        # these variables will have the full specifics of the streamflow source 
        self.ARC_FileName_FloodForecast = self.ARC_Folder / f"{strm_source}_ARC_Input_{self.FileName}_FloodForecast.txt"
        self.FloodDepthFile = self.flood_folder / f"{strm_source}_{self.FileName}_ARC_FloodDepth{self.floodmap_id}.tif"
        self.FloodWSEFile = self.flood_folder / f"{strm_source}_{self.FileName}_ARC_FloodWSE{self.floodmap_id}.tif"
        self.FloodVELFile = self.flood_folder / f"{strm_source}_{self.FileName}_ARC_FloodVEL{self.floodmap_id}.tif"

        if configs.mapper == Mapper.CURVE2FLOOD_FLDPLNPY:
            self.setup_fldpln_files()

    @property
    def config_end(self) -> str:
        """Return the ARC/mapper config extension for this workspace."""
        return 'yaml' if self.configs.use_yaml else 'txt'

    def setup_dem(self, dem: Path | None):
        """Resolve DEM naming and the assigned working DEM path."""
        if dem:
            self.FileName = Path(dem).stem
        elif self.configs.bbox:
            # the 5th decimal place corresponds to ~1m resolution, which is more precise than we need for naming purposes, so we can round to 5 decimal places for cleaner file names
            self.FileName = f"dem_{self.configs.bbox[0]:.5f}_{self.configs.bbox[1]:.5f}_{self.configs.bbox[2]:.5f}_{self.configs.bbox[3]:.5f}"
        else:
            raise ValueError("Must specify either a DEM or a bounding box in the configs.")

        if self.configs.buffer:
            if not self.configs.source_dems:
                raise ValueError(
                    "Buffering requested but no source DEMs assigned."
                )
            self.FileName += '_buffered'
        
        self.original_dem = dem
        if dem and not self.configs.bbox and not self.configs.buffer:
            self.assigned_dem = Path(dem)
        else:
            self.assigned_dem = self.DEM_folder / f"{self.FileName}.{'vrt' if self.configs.use_vrt else 'tif'}"

    def setup_fldpln_files(self):
        """Add FLDPLN-specific working files to the workspace."""
        self.fldpln_library = self.VDT_Folder / 'fldpln_library.parquet'

    def __repr__(self):
        return f"Workspace(watershed={self.watershed}, output_dir={self.output_dir})"
