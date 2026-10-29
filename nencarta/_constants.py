from pathlib import Path

THIS_DIR = Path(__file__).parent
GEOGLOWS_RETURN_PERIODS_URL = 's3://geoglows-v2/retrospective/return-periods.zarr'
GEOGLOWS_FDC_URL = 's3://geoglows-v2/retrospective/fdc.zarr'
GEOGLOWS_DAILY_URL = 's3://geoglows-v2/retrospective/daily.zarr'
GEOGLOWS_FORECAST_PREFIX_URL = 's3://geoglows-v2-forecasts/'
NWM_RP_URL = 'https://nwm-api.ciroh.org/return-period'
CACHED_ESA_GRID = THIS_DIR / '.esa_worldcover_grid.parquet'
# The FloodSpreader mapper runs this script, which is not part of the repository: put it here to use FloodSpreader
FLOODSPREADER_PATH = THIS_DIR / "floodspreader.py"
