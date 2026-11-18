import json
import re
import shutil
import subprocess
from pathlib import Path

from osgeo import gdal

from nencarta.core.floodmapper_output import FloodMapperBulkOutput
from nencarta.logger import LOG
from nencarta.workspace import Workspace

gdal.UseExceptions()

FEET_PER_METER = 1 / 0.3048

# What Go-Consequences prints, carrying on with fewer or no structures, when it can't get them all from the NSI API
_NSI_FAILED = re.compile(r'NSI API request failed|Error unmarshalling JSON record|^Get "', re.MULTILINE)

def _Create_Go_Consequence_GeoJSON(Consequences_JSON_Path, Container_Hazard_Path, Consequences_Output_GPKG_File):
    """
    Write a Go-Consequences config: damage to the National Structure Inventory's structures from a depth raster,
    written to a GeoPackage. The paths are inside the Docker container.
    """
    config = {
        "structure_provider_info": {
            "structure_provider_type": "NSIAPI"
        },
        "hazard_provider_info": {
            "hazards": [
                {
                    "hazard_parameter_type": "depth",
                    "hazard_provider_file_path": Container_Hazard_Path
                }
            ]
        },
        "results_writer_info": {
            "results_writer_type": "GPKG",
            "output_file_path": f"/data/Consequences/{Consequences_Output_GPKG_File}"
        }
    }

    with open(Consequences_JSON_Path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def _hazard_raster(depth_file: Path) -> Path:
    """
    Write the depth map as Go-Consequences reads it, beside it: in WGS84, since it looks up the depth at each
    structure's longitude and latitude without reprojecting, and in feet, since it reads a config's depth raster as
    feet (the unit of its damage functions and of the NSI's foundation heights).
    """
    hazard_file = depth_file.with_name(f"{depth_file.stem}_WGS84_ft{depth_file.suffix}")
    LOG.info(f"Converting depth raster to WGS84 and feet: {depth_file} -> {hazard_file}")
    warp_options = gdal.WarpOptions(format="MEM", dstSRS="EPSG:4326", resampleAlg=gdal.GRA_Bilinear)
    ds: gdal.Dataset = gdal.Warp("", str(depth_file), options=warp_options)
    band: gdal.Band = ds.GetRasterBand(1)
    depth = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    wet = slice(None) if nodata is None else depth != nodata
    depth[wet] *= FEET_PER_METER
    band.WriteArray(depth)
    gdal.GetDriverByName("GTiff").CreateCopy(str(hazard_file), ds, options=["COMPRESS=DEFLATE"])
    return hazard_file

def _get_consequences_tasks(floodmapper_bulk_output: FloodMapperBulkOutput, workspace: Workspace) -> list:
    """
    The Docker commands that run Go-Consequences on each depth map, made WGS84 and feet first, each with the
    GeoPackage it writes.
    """
    if not workspace.configs.estimate_consequences:
        return []

    workspace.Consequences_Folder.mkdir(parents=True, exist_ok=True)

    tasks = []
    for floodmapper_output in floodmapper_bulk_output.outputs:
        depth_file = floodmapper_output.depth_map
        if depth_file is None:
            LOG.warning("No depth file found in floodmapper output, skipping consequences estimation")
            continue

        try:
            depth_file_wgs84 = _hazard_raster(depth_file)
        except RuntimeError as e:
            LOG.error(f"Can't estimate the flood damage of {depth_file}: converting it to WGS84 and feet failed: {e}")
            continue

        # The depth raster's folder and the Consequences folder are mounted separately (see docker_command
        # below) because folder_paths can put them anywhere, not just under output_dir
        Container_Hazard_Path = f"/data/hazard/{depth_file_wgs84.name}"

        Forecast_Flood_Depth_Raster_Name = depth_file_wgs84.name
        Consequences_JSON_File = Forecast_Flood_Depth_Raster_Name.replace('.tif', '_consequences.json') 
        Consequences_JSON_Path = workspace.Consequences_Folder / Consequences_JSON_File
        Consequences_Output_GPKG_File = Consequences_JSON_File.replace('.json', '.gpkg')

        LOG.info(f"Creating consequences file {Consequences_JSON_Path}")
        _Create_Go_Consequence_GeoJSON(Consequences_JSON_Path, Container_Hazard_Path, Consequences_Output_GPKG_File)
        # run the go-consequences Docker container
        hazard_dir = depth_file_wgs84.parent.resolve().as_posix()
        consequences_dir = workspace.Consequences_Folder.resolve().as_posix()

        docker_command = [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={hazard_dir},target=/data/hazard",
            "--mount",
            f"type=bind,source={consequences_dir},target=/data/Consequences",
            "go-consequences:latest",
            f"/data/Consequences/{Consequences_JSON_File}",
        ]
        tasks.append((docker_command, workspace.Consequences_Folder / Consequences_Output_GPKG_File))

    return tasks

def _all_written(results: Path, stdout: str) -> bool:
    """Whether the GeoPackage holds every structure Go-Consequences says it wrote when it closed it."""
    written = re.search(r"Closing, wrote (\d+) features", stdout)
    if written is None or not results.exists():
        return False
    
    try:
        ds: gdal.Dataset = gdal.OpenEx(str(results), gdal.OF_VECTOR)
        layer: gdal.Layer = ds.GetLayerByName("results")
        return layer is not None and layer.GetFeatureCount() == int(written[1])
    except RuntimeError:
        return False

def run_consequences(floodmapper_bulk_output: FloodMapperBulkOutput, workspace: Workspace) -> None:
    """Estimate flood damage on each depth map with the go-consequences Docker image (estimate_consequences)."""
    if workspace.configs.estimate_consequences and shutil.which("docker") is None:
        LOG.error("Can't estimate flood damage: Docker isn't installed or isn't on the PATH (see the installation docs).")
        return
    
    consequences_tasks = _get_consequences_tasks(floodmapper_bulk_output, workspace)
    for docker_command, results in consequences_tasks:
        # So that an earlier run's results can't pass for this one's
        results.unlink(missing_ok=True)
        LOG.info(f"Running consequences estimation with command: {' '.join(docker_command)}")
        result = subprocess.run(docker_command, capture_output=True, text=True, check=False)
        if _NSI_FAILED.search(result.stdout):
            LOG.error(f"Go-Consequences couldn't get the structures from the NSI API, so {results} is missing some "
                      f"or all of them: {result.stdout}")
        elif result.returncode == 0:
            LOG.info(f"Consequences estimation completed successfully: {result.stdout}")
        elif _all_written(results, result.stdout):
            # Go-Consequences closes its results twice, and the second close can crash it after they're written
            LOG.warning(f"Go-Consequences exited with code {result.returncode} after writing all its results to "
                        f"{results}.")
        else:
            LOG.error(f"Error running consequences estimation: {result.stderr}")