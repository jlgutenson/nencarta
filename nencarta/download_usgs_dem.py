import os

import requests

from .logger import LOG

# USGS Base URL for 1/3 Arc-Second DEM Data
USGS_BASE_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current"

def get_dem_filename(lat, lon):
    """Return the USGS tile name, e.g. 'n47w122', and its file name, 'n47w122.tif', for a latitude and longitude."""
    lat_prefix = "n" if lat >= 0 else "s"
    lon_prefix = "e" if lon >= 0 else "w"
    
    lat_int = abs(int(lat))
    lon_int = abs(int(lon))
    
    lat_long_str = f"{lat_prefix}{lat_int:02d}{lon_prefix}{lon_int:03d}"
    filename = f"{lat_prefix}{lat_int:02d}{lon_prefix}{lon_int:03d}.tif"
    return lat_long_str, filename

def download_dem(lat, lon, bad_url_list, save_dir):
    """
    Download the USGS 1/3 arc-second DEM tile for ``lat`` and ``lon`` to ``save_dir``, unless it is already there.

    Returns ``(path, file name, bad_url_list)``; the path is None when the download failed. A tile USGS doesn't
    have (404) is added to ``bad_url_list``, and URLs already in it are skipped.
    """
    lat_long_str, filename = get_dem_filename(lat, lon)
    dem_url = f"{USGS_BASE_URL}/{lat_long_str}/USGS_13_{filename}"
    filename = f"USGS_13_{filename}"
    save_path = os.path.join(save_dir, filename)

    # Check if we have already tried to download this URL and failed because of a 404 error
    if dem_url in bad_url_list:
        LOG.info(f"DEM URL is in the bad URL list: {dem_url}. Skipping download.")
        return (None, None, bad_url_list)

    # Create the save directory if it doesn't exist
    os.makedirs(save_dir, exist_ok=True)

    if os.path.exists(save_path):    
        LOG.info(f"Downloaded {save_path} already so, we will NOT redownload it.")
        DEM_Filename = os.path.basename(save_path)
        return (save_path, DEM_Filename, bad_url_list)

    LOG.info(f"Downloading DEM file: {filename} from {dem_url}")

    try:
        with requests.get(dem_url, stream=True) as response:
            if response.status_code == 404:
                LOG.info(f"DEM not found (404) for {dem_url}. Skipping.")
                bad_url_list.append(dem_url)  # Add to bad URL list
                return (None, None, bad_url_list)
                

            response.raise_for_status()  # Raise for other bad status codes

            total_size = int(response.headers.get('content-length', 0))
            with open(save_path, "wb") as file:
                file.write(response.content)

        # Verify download
        if os.path.exists(save_path) and os.path.getsize(save_path) == total_size:
            LOG.info(f"Download complete: {save_path}")
            DEM_Filename = os.path.basename(save_path)
            return (save_path, DEM_Filename, bad_url_list)
        else:
            LOG.warning(f"Download may be incomplete! Expected: {total_size} bytes, Got: {os.path.getsize(save_path)} bytes")

    except requests.exceptions.RequestException as e:
        LOG.warning(f"Error downloading {dem_url}: {e}")

    return (None, None, bad_url_list)
