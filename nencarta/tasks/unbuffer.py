import os

from osgeo import gdal

from nencarta.core.floodmapper_output import FloodMapperBulkOutput
from nencarta.core.raster import Raster
from nencarta.logger import LOG
from nencarta.workspace import Workspace


def unbuffer_maps(floodmapper_output: FloodMapperBulkOutput, workspace: Workspace) -> None:
    """
    Crop each buffered flood map back to the input DEM's extent (or the bbox, without a DEM). The cropped map
    replaces the buffered one, named without '_buffered'.
    """
    configs = workspace.configs
    if not configs.buffer:
        return 
    
    for file in floodmapper_output.existing_rasters():
        output_file = file.parent / file.name.replace('_buffered_', '_')
        # With short_file_names there is no '_buffered_' to drop, so the map is cropped in place
        in_place = output_file == file

        if not in_place and output_file.exists() and not configs.overwrite_floodmaps:
            LOG.info(f"{output_file} already exists and we aren't making it again...")
            continue
            
        ds: gdal.Dataset = gdal.Open(file)
        width = ds.RasterXSize
        height = ds.RasterYSize
        ds = None

        if workspace.original_dem:
            raster = Raster(workspace.original_dem)
            minx, miny, maxx, maxy = raster.bbox
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
        # GDAL takes (minx, miny, maxx, maxy); giving it maxy first writes the map upside down (south-up)
        options = gdal.WarpOptions(format='GTiff',
                                        outputBounds=(minx, miny, maxx, maxy),
                                        width=unbuffered_width,
                                        height=unbuffered_height,
                                        creationOptions=[f"COMPRESS={configs.compression}", "PREDICTOR=2"]
                                        )
        target = file.with_name(f"{file.stem}_unbuffering{file.suffix}") if in_place else output_file
        ds = gdal.Warp(target, file, options=options)
        ds = None

        if in_place:
            os.replace(target, file)
        else:
            file.unlink()  # Remove the buffered file after unbuffering