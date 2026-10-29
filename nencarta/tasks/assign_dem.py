from pathlib import Path

from osgeo import gdal, osr
from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info

from nencarta.core.raster import Raster
from nencarta.logger import LOG
from nencarta.workspace import Workspace

_WGS84 = osr.SpatialReference()
_WGS84.ImportFromEPSG(4326)

def _apply_buffer(bbox, distance):
    minx, miny, maxx, maxy = bbox
    return (
        minx - distance,
        miny - distance,
        maxx + distance,
        maxy + distance,
    )

def _build_options(bbox, vrt, src_proj, out_proj, xres=None, yres=None):
    option_dict = {
        "resampleAlg": "nearest",
        "outputBounds": bbox,
    }

    if xres is not None:
        option_dict["xRes"] = xres
        option_dict["yRes"] = yres

    if vrt:
        option_dict["outputSRS"] = out_proj
        return gdal.BuildVRT, gdal.BuildVRTOptions(**option_dict)

    option_dict["dstSRS"] = out_proj
    option_dict["outputBoundsSRS"] = src_proj
    return gdal.Warp, gdal.WarpOptions(**option_dict)

def _validate_dem_coordinate_system(dem: Path):
    """Whether the DEM's coordinates are in metres or degrees. Logs the reason when they aren't."""
    unit_aliases = {
        'meter': 'meter',
        'meters': 'meter',
        'metre': 'meter',
        'metres': 'meter',
        'degree': 'degree',
        'degrees': 'degree'
    }
    raster_projections = {
        'DEM': Raster(dem).projection,
    }
    for raster_name, raster_projection in raster_projections.items():
        try:
            raster_crs = CRS.from_wkt(raster_projection)
        except Exception as ex:  # noqa: BLE001
            LOG.error(f'Unable to parse CRS for {raster_name} raster: {ex}')
            return False

        axis_units = {(axis.unit_name or '').strip().lower() for axis in raster_crs.axis_info if axis is not None}
        axis_units.discard('')
        if not axis_units:
            LOG.error(f'Unable to determine CRS units for {raster_name} raster.')
            return False

        invalid_units = [u for u in sorted(axis_units) if unit_aliases.get(u) not in {'meter', 'degree'}]
        if invalid_units:
            LOG.error(f'{raster_name} raster CRS units are not meters or degrees: {", ".join(invalid_units)}')
            return False
        
    return True

def assign_and_validate_dem(workspace: Workspace) -> Path:
    """
    Make the DEM the workspace maps (workspace.assigned_dem): the input DEM itself, checked to be in metres or
    degrees, or a DEM cut from source_dems to bbox, or to the input DEM's extent grown by buffer_distance.
    project_to_utm projects a cut DEM to its UTM zone.
    """
    configs = workspace.configs
    if workspace.original_dem == workspace.assigned_dem and workspace.assigned_dem.exists() and not configs.overwrite:
        LOG.info(f"{workspace.assigned_dem} exists")
        return workspace.assigned_dem

    if configs.buffer and not configs.source_dems:
        raise ValueError(
            "Buffering requested but no source DEMs assigned."
        )

    if workspace.original_dem and configs.bbox:
        target_bbox = configs.bbox
    elif workspace.original_dem and configs.buffer:
        raster = Raster(workspace.original_dem)
        target_bbox = raster.epsg_4326_bbox
    elif configs.bbox:
        target_bbox = configs.bbox
    elif workspace.original_dem:
        if not _validate_dem_coordinate_system(workspace.assigned_dem):
            raise ValueError("Original DEM has invalid coordinate system. See log for details.")
        LOG.info("Using original DEM as the assigned DEM.")
        return workspace.assigned_dem
    else:
        raise ValueError(
            "Must specify either a DEM or a bounding box."
        )

    if configs.buffer:
        target_bbox = _apply_buffer(target_bbox, configs.buffer_distance)

    if workspace.original_dem:
        candidates = configs.source_dems + [workspace.original_dem]

        surrounding_dems = Raster.get_rasters_in_extent(
            target_bbox,
            candidates,
        )

        assert workspace.original_dem in surrounding_dems

    else:
        surrounding_dems = Raster.get_rasters_in_extent(
            target_bbox,
            configs.source_dems,
        )

        if not surrounding_dems:
            if configs.raise_errors_if_nothing_in_domain:
                raise ValueError(
                    "No source DEMs intersect the specified bounding box."
                )
            
            return None

    xres = yres = None

    if workspace.original_dem:
        raster = Raster(workspace.original_dem)
        xres, yres = raster.resolution
    else:
        # Use the resolution of the first intersecting DEM as the target resolution
        # This avoids little gaps when stitching rasters files together
        raster = Raster(surrounding_dems[0])
        xres, yres = raster.resolution

    if configs.project_to_utm:
        if raster.is_in_degrees:
            xres = None
            yres = None

        # Center of raster
        gt = raster.geotransform
        height, width = raster.shape
        x = gt[0] + (width / 2) * gt[1] + (height / 2) * gt[2]
        y = gt[3] + (width / 2) * gt[4] + (height / 2) * gt[5]

        srs = osr.SpatialReference(raster.projection)
        if not srs.IsSame(_WGS84):
            transform = osr.CoordinateTransformation(srs, _WGS84)
            lon, lat, _ = transform.TransformPoint(x, y)
        else:
            lon, lat = x, y

        crs = query_utm_crs_info(
            datum_name="WGS 84",
            area_of_interest=AreaOfInterest(
                west_lon_degree=lon,
                south_lat_degree=lat,
                east_lon_degree=lon,
                north_lat_degree=lat,
            ),
        )[0].code
        crs = CRS.from_epsg(crs)
        out_proj = crs.to_wkt()
    else:
        out_proj = raster.projection

    builder, options = _build_options(
        target_bbox,
        vrt=(workspace.assigned_dem.suffix == '.vrt'),
        src_proj=raster.projection,
        out_proj=out_proj,
        xres=xres,
        yres=yres,
    )

    workspace.assigned_dem.parent.mkdir(parents=True, exist_ok=True)
    builder(workspace.assigned_dem, surrounding_dems, options=options)

    LOG.info(f"Assigned DEM created at {workspace.assigned_dem} with bounding box {target_bbox} and resolution ({xres}, {yres}).")

    return workspace.assigned_dem
