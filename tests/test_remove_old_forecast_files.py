from datetime import datetime, timedelta, timezone

from nencarta.core.configs import NencartaConfig
from nencarta.tasks.flow_files import remove_old_forecast_files
from nencarta.workspace import Workspace


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")


def make_workspace(workspace_tmp, **overrides) -> Workspace:
    dem = workspace_tmp / "dems" / "fabdem.tif"
    dem.parent.mkdir(exist_ok=True)
    dem.touch()
    config = {"name": "example", "dem": str(dem), "flowline": str(workspace_tmp / "streams.gpkg"),
              "output_dir": str(workspace_tmp / "out"), "remove_old_forecast_files": True, "age_of_forecast_days": 7}
    config.update(overrides)
    workspace = Workspace(NencartaConfig(config), dem)
    workspace.flood_folder.mkdir(parents=True)
    return workspace


def remaining_after_removal(workspace: Workspace, names: list[str]) -> list[str]:
    for name in names:
        (workspace.flood_folder / name).touch()
    remove_old_forecast_files(workspace)
    return sorted(path.name for path in workspace.flood_folder.iterdir())


def test_old_forecast_maps_and_the_files_made_from_them_are_deleted(workspace_tmp):
    workspace = make_workspace(workspace_tmp)
    workspace.configs.forecastdate = days_ago(0)
    old = days_ago(10)
    deleted = [
        f"GEOGLOWS_fabdem_ARC_FloodForecast_{old}.tif", f"GEOGLOWS_fabdem_ARC_FloodForecast_{old}.gpkg",
        f"GEOGLOWS_fabdem_ARC_FloodForecast_{old}.tif.aux.xml", f"GEOGLOWS_fabdem_ARC_FloodDepthForecast_{old}.tif",
        f"GEOGLOWS_fabdem_ARC_FloodDepthForecast_{old}_WGS84.tif", f"GEOGLOWS_fabdem_ARC_FloodWSEForecast_{old}.tif",
        f"GEOGLOWS_fabdem_ARC_FloodVELForecast_{old}.tif",
    ]
    kept = [
        f"GEOGLOWS_fabdem_ARC_FloodForecast_{days_ago(6)}.tif",  # not old enough
        f"GEOGLOWS_fabdem_ARC_FloodForecast_{days_ago(0)}.tif",  # the forecast being mapped
        f"GEOGLOWS_other_ARC_FloodForecast_{old}.tif",  # another DEM's
        "GEOGLOWS_fabdem_ARC_Flood_rp100.tif",  # a return period map
        f"GEOGLOWS_fabdem_ARC_Flood_myForecast_{old}.tif",  # the map of a user flow file named myForecast_<date>
    ]

    assert remaining_after_removal(workspace, deleted + kept) == sorted(kept)


def test_the_forecast_being_mapped_is_kept_however_old(workspace_tmp):
    mapped, older = days_ago(30), days_ago(40)
    workspace = make_workspace(workspace_tmp, forensic_forecast_date=mapped, floodmap_identifier="run")
    names = [f"GEOGLOWS_fabdem_ARC_Flood_runForecast_{mapped}.tif", f"GEOGLOWS_fabdem_ARC_Flood_runForecast_{older}.tif"]

    assert remaining_after_removal(workspace, names) == names[:1]


def test_old_nwm_forecast_maps_are_deleted_whatever_their_hour(workspace_tmp):
    workspace = make_workspace(workspace_tmp, streamflow_source="NWM_short_range", nwm_api_key="key")
    workspace.configs.forecastdate, workspace.configs.forecasthour = days_ago(0), "06"
    old = days_ago(10)
    deleted = [
        f"NWM_short_range_fabdem_ARC_Flood_NWM_short_range_ARC_FloodForecast_{old}_06.tif",
        f"NWM_short_range_fabdem_ARC_FloodDepth_NWM_short_range_ARC_FloodForecast_{old}_18_WGS84.tif",
    ]
    kept = [f"NWM_short_range_fabdem_ARC_Flood_NWM_short_range_ARC_FloodForecast_{days_ago(1)}_06.tif"]

    assert remaining_after_removal(workspace, deleted + kept) == sorted(kept)


def test_short_file_names(workspace_tmp):
    workspace = make_workspace(workspace_tmp, short_file_names=True)
    workspace.configs.forecastdate = days_ago(0)
    names = [f"ARC_FloodForecast_{days_ago(10)}.tif", f"ARC_FloodForecast_{days_ago(1)}.tif"]

    assert remaining_after_removal(workspace, names) == names[1:]
