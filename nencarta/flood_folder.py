import os
import re

class FloodFolder:
    def __init__(self, configs: dict):
        self.watershed = configs.get('name')
        self.output_dir = configs.get('output_dir')
        self.dem_folder = configs.get('dem_dir')
        self.mapper = configs.get('mapper', 'FloodSpreader')

        self.ARC_Folder = self._create_and_get_folder('ARC_InputFiles')
        self.flood_folder = self._create_and_get_folder('FloodMap')
        self.bathy_file_folder = self._create_and_get_folder('Bathymetry')
        self.dem_updated_folder = self._create_and_get_folder('DEM_Updated')
        self.strm_folder = self._create_and_get_folder('STRM')
        self.land_folder = self._create_and_get_folder('LAND')
        self.FLOW_Folder = self._create_and_get_folder('FLOW')
        self.VDT_Folder = self._create_and_get_folder('VDT')
        self.ESA_LC_Folder = self._create_and_get_folder('ESA_LC')
        self.FIST_Folder = self._create_and_get_folder('FIST')
        self.Consequences_Folder = self._create_and_get_folder('Consequences')
        self.Flow_Direction_Folder = self._create_and_get_folder('FlowDirection')

        if configs.get('mannings_text_file'):
            if not os.path.isfile(configs.get('mannings_text_file')):
                raise FileNotFoundError(f"Provided Manning's n text file not found: {configs.get('mannings_text_file')}")
            self.mannings_n_text_file = configs.get('mannings_text_file')
        else:
            self.mannings_n_text_file = os.path.join(self.land_folder, 'AR_Manning_n_MED.txt')

        self.floodmap_mode = configs.get('floodmap_mode', 'forecast')

    def _create_and_get_folder(self, subfolder_name: str) -> str:
        folder_path = os.path.join(self.output_dir, self.watershed, subfolder_name)
        os.makedirs(folder_path, exist_ok=True)
        return folder_path

    def setup_folder_for_dem(self, DEM: str, watershed_dict: dict):
        self.FileName = DEM.replace('.tif','').replace('.img','')
        self.DEM_File = os.path.join(self.dem_folder, DEM)

        # currently the land file will be the same regardless of the streamflow source
        self.LAND_File = os.path.join(self.land_folder, self.FileName + '_LAND_Raster.tif')

        #Datasets to be Created
        streamflow_source = watershed_dict['streamflow_source']
        self.DEM_StrmShp = os.path.join(self.strm_folder, f"{streamflow_source}_{self.FileName}_StrmShp.gpkg")
        self.DEM_Reanalsyis_FlowFile = os.path.join(self.FLOW_Folder,f"{streamflow_source}_{self.FileName}_Reanalysis.csv")
        self.COMID_Q_File = os.path.join(os.path.dirname(self.DEM_Reanalsyis_FlowFile), f"{os.path.basename(self.DEM_File[:-4])}_2yr_flow_initial.txt")

        # isolating the NWM or GEOGLOWS text in the streamflow_source variable
        match = re.search(r"(NWM|GEOGLOWS)", streamflow_source)
        # these will only vary based upon if they are NWM or GEOGLOWS
        self.ARC_FileName_Bathy = os.path.join(self.ARC_Folder, f"{match.group(0)}_ARC_Input_{self.FileName}_Bathy.txt")
        self.ARC_FileName_Initial = os.path.join(self.ARC_Folder, f"{match.group(0)}_ARC_Input_{self.FileName}_InitialFlood.txt")
        self.DEM_File_Clean = os.path.join(self.dem_updated_folder, f"{self.FileName}_Clean.tif") if watershed_dict['clean_dem'] else self.DEM_File
        self.VDT_Test_File = os.path.join(self.VDT_Folder, f"{match.group(0)}_{self.FileName}_VDT_FS.csv")
        self.VDT_Test_File_Bathy = self.VDT_Test_File.replace(".csv", "_Bathy.csv")
        self.STRM_File = os.path.join(self.strm_folder, f"{match.group(0)}_{self.FileName}_STRM_Raster.tif")
        self.STRM_File_Clean = self.STRM_File.replace('.tif','_Clean.tif')

        vdt_ext = watershed_dict['vdt_file_extension']
        self.VDT_File = os.path.join(self.VDT_Folder, f"{match.group(0)}_{self.FileName}_VDT_Database.{vdt_ext}")
        self.VDT_File_Initial = self.VDT_File.replace(f".{vdt_ext}", f"_Initial.{vdt_ext}")
        self.VDT_File_Bathy = self.VDT_File.replace(f".{vdt_ext}", f"_Bathy.{vdt_ext}")

        self.AP_File = self.VDT_File.replace("VDT_", "AP_").replace(f".{vdt_ext}", f"_Bathy.txt")

        self.Curve_File = os.path.join(self.VDT_Folder, f"{match.group(0)}_{self.FileName}_CurveFile.csv")
        self.Curve_File_Initial = self.Curve_File.replace(".csv", "_Initial.csv")
        self.Curve_File_Bathy = self.Curve_File.replace(".csv", "_Bathy.csv")

        self.LU_and_Streams_Water_Map = os.path.join(self.flood_folder, f"{match.group(0)}_{self.FileName}_ARC_Flood_Initial.tif")
        self.DepthMapFile = os.path.join(self.flood_folder, f"{match.group(0)}_{self.FileName}_ARC_Depth.tif")
        self.ARC_BathyFile = os.path.join(self.bathy_file_folder, f"{match.group(0)}_{self.FileName}_ARC_Bathy.tif")
        self.FS_BathyFile = os.path.join(self.bathy_file_folder, f"{match.group(0)}_{self.FileName}_FS_Bathy.tif")  

        self.floodmap_id = watershed_dict.get('floodmap_identifier', '')
        if self.floodmap_id:
            self.floodmap_id = f"_{self.floodmap_id}"
        else:
            self.floodmap_id = ''

        self.FloodMapFile = os.path.join(self.flood_folder, f"{match.group(0)}_{self.FileName}_ARC_Flood{self.floodmap_id}.tif")
        self.FloodMapFile_Initial = self.FloodMapFile.replace('.tif', '_Initial.tif')
        self.FloodMapFile_Initial_SHP = self.FloodMapFile.replace('.tif', '_Initial.shp')
        self.FloodMapFile_Bathy = self.FloodMapFile.replace('.tif', '_Bathy.tif')
        self.FloodMapFile_Bathy_SHP = self.FloodMapFile.replace('.tif', '_Bathy.shp')

        # these variables will have the full specifics of the streamflow source 
        self.ARC_FileName_FloodForecast = os.path.join(self.ARC_Folder, f"{streamflow_source}_ARC_Input_{self.FileName}_FloodForecast.txt")
        self.FloodDepthFile = os.path.join(self.flood_folder, f"{streamflow_source}_{self.FileName}_ARC_FloodDepth{self.floodmap_id}.tif")
        self.FloodWSEFile = os.path.join(self.flood_folder, f"{streamflow_source}_{self.FileName}_ARC_FloodWSE{self.floodmap_id}.tif") 
        self.FloodVELFile = os.path.join(self.flood_folder, f"{streamflow_source}_{self.FileName}_ARC_FloodVEL{self.floodmap_id}.tif")

    def set_source_landcover_files(self, LandCoverFiles: list[str]):
        self.LandCoverFiles = LandCoverFiles

    def setup_fldpln_files(self):
        self.filled_dem = os.path.join(self.Flow_Direction_Folder, os.path.basename(self.DEM_File).replace('.tif','_filled.tif'))
        self.flowdir = os.path.join(self.Flow_Direction_Folder, os.path.basename(self.DEM_File).replace(".tif","_flowdir.tif"))
        self.flowacc = os.path.join(self.Flow_Direction_Folder, os.path.basename(self.DEM_File).replace(".tif","_flowacc.tif"))
        self.new_StrmShp = os.path.join(self.Flow_Direction_Folder, os.path.basename(self.DEM_File).replace(".tif","_flowlines.gpkg"))
        self.new_catchment = os.path.join(self.Flow_Direction_Folder, os.path.basename(self.DEM_File).replace(".tif","_catchments.gpkg"))
        self.new_StrmShp_matched = os.path.join(self.strm_folder, os.path.basename(self.DEM_File).replace(".tif","_flowlines_matched.gpkg"))

    def setup_flood_forecast_files(self, Forecast_Flood_Map: str, Forecast_Flood_Depth_Raster: str, ForecastFlowFile: str):
        self.Forecast_Flood_Map = Forecast_Flood_Map
        self.Forecast_Flood_Depth_Raster = Forecast_Flood_Depth_Raster
        self.ForecastFlowFile = ForecastFlowFile

    def setup_flood_user_files(self, Flood_Maps: list[str], Depth_Maps: list[str], UserFlowFiles: list[str], Model_Input_Files: list[str]):
        self.User_Flood_Maps = Flood_Maps
        self.User_Depth_Maps = Depth_Maps
        self.UserFlowFiles = UserFlowFiles
        self.Model_Input_Files = Model_Input_Files

    def get_flow_files(self) -> list[str]:
        if self.floodmap_mode == 'forecast':
            return [self.ForecastFlowFile]
        elif self.floodmap_mode == 'user':
            return self.UserFlowFiles
        else:
            raise NotImplementedError(f"Floodmap mode '{self.floodmap_mode}' is not implemented.")
        
    def get_depth_files(self):
        if self.floodmap_mode == 'forecast':
            return [self.Forecast_Flood_Depth_Raster]
        elif self.floodmap_mode == 'user':
            return self.User_Depth_Maps
        else:
            raise NotImplementedError(f"Floodmap mode '{self.floodmap_mode}' is not implemented.")
