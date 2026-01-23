import os
import re

class FloodFolder:
    def __init__(self, configs: dict):
        self.watershed = configs.get('name')
        self.output_dir = configs.get('output_dir')
        self.dem_folder = configs.get('dem_dir')

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

        self.mannings_n_text_file = os.path.join(self.land_folder, 'AR_Manning_n_MED.txt')
        self.flowdir_orig = ''
        self.flowdir_bathy = ''

    def _create_and_get_folder(self, subfolder_name: str) -> str:
        folder_path = os.path.join(self.output_dir, self.watershed, subfolder_name)
        os.makedirs(folder_path, exist_ok=True)
        return folder_path

    def setup_folder_for_dem(self, DEM: str, streamflow_source: str, clean_dem: bool):
        self.FileName = DEM.replace('.tif','').replace('.img','')
        self.DEM_File = os.path.join(self.dem_folder, DEM)

        # currently the land file will be the same regardless of the streamflow source
        self.LAND_File = os.path.join(self.land_folder, self.FileName + '_LAND_Raster.tif')

        #Datasets to be Created
        self.DEM_StrmShp = os.path.join(self.strm_folder, f"{streamflow_source}_{self.FileName}_StrmShp.gpkg")
        self.DEM_Reanalsyis_FlowFile = os.path.join(self.FLOW_Folder,f"{streamflow_source}_{self.FileName}_Reanalysis.csv")
        self.COMID_Q_File = os.path.join(os.path.dirname(self.DEM_Reanalsyis_FlowFile), f"{os.path.basename(self.DEM_File[:-4])}_2yr_flow_initial.txt")

        # isolating the NWM or GEOGLOWS text in the streamflow_source variable
        match = re.search(r"(NWM|GEOGLOWS)", streamflow_source)
        # these will only vary based upon if they are NWM or GEOGLOWS
        self.ARC_FileName_Bathy = os.path.join(self.ARC_Folder, match.group(0) + '_ARC_Input_' + self.FileName + '_Bathy.txt')
        self.ARC_FileName_Initial = os.path.join(self.ARC_Folder, match.group(0) + '_ARC_Input_' + self.FileName + '_InitialFlood.txt')
        self.DEM_File_Clean = os.path.join(self.dem_updated_folder, match.group(0) + '_' + self.FileName + '_Clean.tif') if clean_dem else self.DEM_File
        self.VDT_Test_File = os.path.join(self.VDT_Folder, match.group(0) + '_' + self.FileName + '_VDT_FS.csv')
        self.STRM_File = os.path.join(self.strm_folder, match.group(0) + '_' + self.FileName + '_STRM_Raster.tif')
        self.STRM_File_Clean = self.STRM_File.replace('.tif','_Clean.tif')
        self.VDT_File = os.path.join(self.VDT_Folder, match.group(0) + '_' + self.FileName + '_VDT_Database.txt')
        self.VDT_File_Bathy = self.VDT_File.replace('.txt', '_Bathy.txt')
        self.Curve_File = os.path.join(self.VDT_Folder, match.group(0) + '_' + self.FileName + '_CurveFile.csv')
        self.FloodMapFile_Initial = os.path.join(self.flood_folder, match.group(0) + '_' + self.FileName + '_ARC_Flood_Initial.tif')
        self.DepthMapFile = os.path.join(self.flood_folder, match.group(0) + '_' + self.FileName + '_ARC_Depth.tif')
        self.ARC_BathyFile = os.path.join(self.bathy_file_folder, match.group(0) + '_' + self.FileName + '_ARC_Bathy.tif')
        self.FS_BathyFile = os.path.join(self.bathy_file_folder, match.group(0) +'_' +  self.FileName + '_FS_Bathy.tif')  
        self.FloodMapFile = os.path.join(self.flood_folder, match.group(0) + '_' + self.FileName + '_ARC_Flood.tif')


        # these variables will have the full specifics of the streamflow source 
        self.ARC_FileName_FloodForecast = os.path.join(self.ARC_Folder, streamflow_source + '_ARC_Input_' + self.FileName + '_FloodForecast.txt')
        self.FloodDepthFile = os.path.join(self.flood_folder, streamflow_source + '_' + self.FileName + '_ARC_FloodDepth.tif')
        self.FloodWSEFile = os.path.join(self.flood_folder, streamflow_source + '_' + self.FileName + '_ARC_FloodWSE.tif') 
        self.FloodVELFile = os.path.join(self.flood_folder, streamflow_source + '_' + self.FileName + '_ARC_FloodVEL.tif')

    def set_landcover_file(self, LandCoverFile: str):
        self.LandCoverFile = LandCoverFile

    def setup_fldpln_files(self):
        self.flowdir_orig = os.path.join(self.Flow_Direction_Folder, os.path.basename(self.DEM_File).replace(".tif","_flowdir_orig_crs.tif"))
        self.flowdir_bathy = os.path.join(self.Flow_Direction_Folder, os.path.basename(self.FS_BathyFile).replace('.tif','_FlowDir.tif'))

    def setup_flood_forecast_files(self, Forecast_Flood_Map: str, Forecast_Flood_Depth_Raster: str, ForecastFlowFile: str):
        self.Forecast_Flood_Map = Forecast_Flood_Map
        self.Forecast_Flood_Depth_Raster = Forecast_Flood_Depth_Raster
        self.ForecastFlowFile = ForecastFlowFile
    
