from osgeo import gdal

from nencarta.logger import LOG
from nencarta.core.raster import Raster
from nencarta.workspace import Workspace
from nencarta.core.floodmapper_output import FloodMapperBulkOutput

def unbuffer_maps(floodmapper_output: FloodMapperBulkOutput, workspace: Workspace) -> None:
    """Crop a buffered flood map back to the source DEM extent and remove ocean cells."""
    configs = workspace.configs
    if not configs.buffer:
        return 
    
    for file in floodmapper_output.existing_rasters():
        output_file = file.parent / file.name.replace('_buffered_', '_')
        if output_file == file:
            LOG.warning(f"Output file {output_file} has the same name as the input file {file}.")

        if output_file.exists() and not configs.overwrite_floodmaps:
            LOG.info(f"{output_file} already exists and we aren't making it again...")
            continue
            
        ds: gdal.Dataset = gdal.Open(file)
        width = ds.RasterXSize
        height = ds.RasterYSize
        ds = None

        if workspace.original_dem:
            raster = Raster(workspace.original_dem)
            minx, miny, maxx, maxy = raster.bbox
            raster.shape
            unbuffered_height, unbuffered_width = raster.shape
        else:
            # Calculate from bbox
            raster = Raster(workspace.assigned_dem)
            gt = raster.geotransform
            minx, miny, maxx, maxy = raster.bbox

            minx += configs.buffer_distance
            miny += configs.buffer_distance
            maxx -= configs.buffer_distance
            maxy -= configs.buffer_distance

            unbuffered_width = int((maxx - minx) / gt[1])
            unbuffered_height = int((maxy - miny) / abs(gt[5]))

        if unbuffered_width == width and unbuffered_height == height:
            continue

        LOG.info(f"Unbuffering flood map {file} to {output_file}...")
        options = gdal.WarpOptions(format='GTiff',
                                        outputBounds=(minx, maxy, maxx, miny),
                                        width=unbuffered_width,
                                        height=unbuffered_height,
                                        creationOptions=[f"COMPRESS={configs.compression}", "PREDICTOR=2"]
                                        )
        gdal.Warp(output_file, file, options=options)

        file.unlink()  # Remove the buffered file after unbuffering