import warnings
from pathlib import Path

import numpy as np
from numba import njit
from osgeo import gdal, ogr

from nencarta.core.raster import Raster
from nencarta.logger import LOG
from nencarta.workspace import Workspace


@njit(cache=True)
def _clean_stream_raster_array(array: np.ndarray) -> tuple[np.ndarray, int, int]:
    """
    Thin rasterized streams: remove cells that stick out of a stream, then cells that make a reach two cells
    wide at a bend. ``array`` has a one-cell border of zeros, and is thinned in place, with its cells below 0 set to 0.
    Returns it and the cells each kind of pass removed.
    """
    nrows, ncols = array.shape
    num_nonzero = 0
    for r in range(nrows):
        for c in range(ncols):
            if array[r, c] > 0:
                num_nonzero += 1
            else:
                array[r, c] = 0

    RR = np.empty(num_nonzero, np.int64)
    CC = np.empty(num_nonzero, np.int64)
    k = 0
    for r in range(nrows):
        for c in range(ncols):
            if array[r, c] > 0:
                RR[k] = r
                CC[k] = c
                k += 1

    first_pass = 0
    second_pass = 0
    for _ in range(2):
        #First pass is just to get rid of single cells hanging out not doing anything
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1

            r=RR[x]
            c=CC[x]
            V = array[r,c]
            if V>0:
                #Left and Right cells are zeros
                #The bottom cells are all zeros as well, but there is a cell directly above that is legit
                if array[r,c+1]==0 and array[r,c-1]==0 and ((array[r+1,c-1]+array[r+1,c]+array[r+1,c+1])==0 and array[r-1,c]>0 or (array[r-1,c-1]+array[r-1,c]+array[r-1,c+1])==0 and array[r+1,c]>0):
                    array[r,c] = 0
                    first_pass += 1
                #top and bottom cells are zeros
                if array[r,c]>0 and array[r+1,c]==0 and array[r-1,c]==0 and ((array[r+1,c+1]+array[r,c+1]+array[r-1,c+1])==0 and array[r,c-1]>0 or (array[r+1,c-1]+array[r,c-1]+array[r-1,c-1])==0 and array[r,c+1]>0):
                    #All cells on the right are zero, but there is a cell to the left that is legit
                    array[r,c] = 0
                    first_pass += 1
        
        #This pass is to remove all the redundant cells
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
                
            r=RR[x]
            c=CC[x]
            V = array[r,c]
            if V>0:
                if array[r+1,c]==V and (array[r+1,c+1]==V or array[r+1,c-1]==V):
                    if sum(array[r+1,c-1:c+2])==0:
                        array[r+1,c] = 0
                        second_pass += 1
                elif array[r-1,c]==V and (array[r-1,c+1]==V or array[r-1,c-1]==V):
                    if sum(array[r-1,c-1:c+2])==0:
                        array[r-1,c] = 0
                        second_pass += 1
                elif array[r,c+1]==V and (array[r+1,c+1]==V or array[r-1,c+1]==V):
                    if sum(array[r-1:r+1,c+2])==0:
                        array[r,c+1] = 0
                        second_pass += 1
                elif array[r,c-1]==V and (array[r+1,c-1]==V or array[r-1,c-1]==V) and sum(array[r-1:r+1,c-2])==0:
                    array[r,c-1] = 0
                    second_pass += 1

    return array, first_pass, second_pass

def _clean_stream_raster(stream_ds: gdal.Dataset) -> None:
    """Thin the streams of an open stream raster, in place."""
    shape = (stream_ds.RasterYSize + 2, stream_ds.RasterXSize + 2)
    array = np.zeros(shape, dtype=np.int32)  # the raster's own type
    array[1:-1, 1:-1] = stream_ds.ReadAsArray()

    array, first_pass, second_pass = _clean_stream_raster_array(array)
    LOG.info(f'First Pass - Removed {first_pass} cells.')
    LOG.info(f'Second Pass - Removed {second_pass} cells.')
    
    # Write the cleaned array to the raster
    stream_ds.WriteArray(array[1:-1, 1:-1])

def make_stream_raster(workspace: Workspace) -> Path:
    """
    Rasterize the domain's streams onto the DEM's grid (workspace.STRM_File_Clean), each cell holding its reach's
    ID, and thin them.
    """
    configs = workspace.configs
    if workspace.STRM_File_Clean.exists() and not configs.overwrite:
        LOG.info(f"{workspace.STRM_File_Clean} already exists and we aren't making it again...")
        return workspace.STRM_File_Clean
    
    if not workspace.DEM_StrmShp.exists() and not configs.raise_errors_if_nothing_in_domain:
        return None
    
    LOG.info("Creating stream raster from stream geometry...")
    workspace.STRM_File_Clean.parent.mkdir(parents=True, exist_ok=True)

    warnings.filterwarnings(
        "once",
        message="Failed to fetch spatial reference on layer.*",
        category=RuntimeWarning,
    )
    
    dem_raster = Raster(workspace.assigned_dem)
    driver: gdal.Driver = gdal.GetDriverByName('GTiff')

    if workspace.STRM_File_Clean.exists():
        try:
            delete_failed = driver.Delete(workspace.STRM_File_Clean) != 0
        except (PermissionError, RuntimeError, OSError):
            delete_failed = True

        if delete_failed:
            raise PermissionError(
                f"Unable to overwrite stream raster because it is locked: {workspace.STRM_File_Clean}"
            )

    source_ds: gdal.Dataset = gdal.OpenEx(workspace.DEM_StrmShp, gdal.OF_VECTOR)
    if source_ds is None:
        raise RuntimeError(f"Unable to open stream geometry: {workspace.DEM_StrmShp}")

    layer: ogr.Layer = source_ds.GetLayer(0)
    layer_name = layer.GetName() if layer is not None else None

    stream_ds: gdal.Dataset = driver.Create(workspace.STRM_File_Clean, dem_raster.ncols, dem_raster.nrows, 1, gdal.GDT_Int32, ['COMPRESS=DEFLATE', 'PREDICTOR=2'])
    stream_ds.SetGeoTransform(dem_raster.geotransform)
    stream_ds.SetProjection(dem_raster.projection)
    options = gdal.RasterizeOptions(
        attribute=configs.stream_id_field,
        layers=[layer_name] if layer_name and workspace.DEM_StrmShp.suffix.lower().endswith(".gpkg") else None,
    )
    gdal.Rasterize(stream_ds, source_ds, options=options)
    source_ds = None

    # Clean the raster
    _clean_stream_raster(stream_ds)
    # Closed here, not left to `stream_ds = None`: the call that compiles _clean_stream_raster_array leaves numba
    # holding its callers' frames, and so this dataset, until the next garbage collection. Until then the raster isn't
    # written, and the water mask and ARC read it empty.
    stream_ds.Close()

    return workspace.STRM_File_Clean
