from pathlib import Path

import pytest

from nencarta.core.configs import NencartaConfig
from nencarta.core.defaults import (
    DEFAULT_ARC_ARGS,
    DEFAULT_CONFIG,
    DEFAULT_CURVE2FLOOD_ARGS,
)
from nencarta.core.enumerations import FloodMapMode, Mapper, StreamflowSource
from nencarta.workspace import Workspace


def base_config(workspace_tmp: Path, **overrides) -> dict:
    dem_dir = workspace_tmp / "dems"
    dem_dir.mkdir(exist_ok=True)
    config = {
        "name": "example",
        "flowline": str(workspace_tmp / "streams.parquet"),
        "dem_dir": str(dem_dir),
        "output_dir": str(workspace_tmp / "out"),
        "mapper": "Curve2Flood-Kernel Weighted",
        "streamflow_source": "GEOGLOWS",
        "use_specified_depth_for_bathy_mask": True,
        "specify_depths_for_bathy_mask": [0.1],
        "clean_dem": False,
        "make_fist_inputs": False,
    }
    config.update(overrides)
    return config


def test_config_normalizes_core_options(workspace_tmp):
    config = NencartaConfig(base_config(workspace_tmp))

    assert config.watershed_name == "example"
    assert config.mapper == Mapper.CURVE2FLOOD_KERNEL_WEIGHTED
    assert config.streamflow_source == StreamflowSource.GEOGLOWS_V2
    assert config.floodmap_mode == FloodMapMode.FORECAST
    assert config.dem_filter == "*"
    assert config.overwrite is False


def test_config_accepts_overwrite_option(workspace_tmp):
    config = NencartaConfig(base_config(workspace_tmp, overwrite=True))

    assert config.overwrite is True


def test_config_uses_shared_defaults_for_bathymetry_options(workspace_tmp):
    config = NencartaConfig(base_config(workspace_tmp))

    assert config.use_dem_derived_channel_mask is DEFAULT_CONFIG["use_dem_derived_channel_mask"]
    # The models' own settings: nencarta's starting values
    assert config.arc_args == DEFAULT_ARC_ARGS
    assert config.curve2flood_args == DEFAULT_CURVE2FLOOD_ARGS
    assert config.fldpln_library_args == {}
    assert config.standard_step is False and config.flood_bathy_mask is False


@pytest.mark.parametrize("value, expected", [(None, None), ("auto", None), ("yes", True), ("No", False), (True, True),
                                             (False, False)])
def test_whether_the_dem_flattens_its_rivers_is_yes_no_or_left_to_its_flat_water(workspace_tmp, value, expected):
    config = base_config(workspace_tmp) if value is None else base_config(workspace_tmp, dem_flattens_rivers=value)

    assert NencartaConfig(config).dem_flattens_rivers is expected


def test_dem_flattens_rivers_other_than_auto_yes_or_no_raises(workspace_tmp):
    with pytest.raises(ValueError, match="dem_flattens_rivers"):
        NencartaConfig(base_config(workspace_tmp, dem_flattens_rivers="sometimes"))


def test_clean_dem_requires_two_bathy_mask_depths(workspace_tmp):
    with pytest.raises(ValueError, match="specify_depths_for_bathy_mask"):
        NencartaConfig(base_config(workspace_tmp, clean_dem=True))


@pytest.mark.parametrize("source", ["GEOGLOWS", "geoglows", "GEOGLOWS_V2", "geoglows_v2"])
def test_geoglows_means_geoglows_v2_and_its_files_keep_their_names(workspace_tmp, source):
    dem = workspace_tmp / "dems" / "fabdem.tif"
    config = NencartaConfig(base_config(workspace_tmp, streamflow_source=source))

    assert config.streamflow_source == StreamflowSource.GEOGLOWS_V2
    assert (config.stream_id_field, config.downstream_id_field) == ("LINKNO", "DSLINKNO")
    assert Workspace(config, dem).DEM_StrmShp.name == "GEOGLOWS_fabdem_StrmShp.gpkg"


def custom_config(workspace_tmp, **overrides):
    settings = dict(streamflow_source="custom", stream_id_field="ID", downstream_id_field="DS_ID",
                    floodmap_mode="user", user_flow_files=[str(workspace_tmp / "flows.csv")],
                    return_period_file=str(workspace_tmp / "rp.nc"))
    return base_config(workspace_tmp, **{**settings, **overrides})


def test_a_custom_source_takes_everything_from_your_files(workspace_tmp):
    config = NencartaConfig(custom_config(workspace_tmp))
    assert config.streamflow_source == StreamflowSource.CUSTOM
    assert (config.stream_id_field, config.downstream_id_field) == ("ID", "DS_ID")
    assert Workspace(config, workspace_tmp / "dems" / "fabdem.tif").DEM_StrmShp.name == "CUSTOM_fabdem_StrmShp.gpkg"

    with pytest.raises(ValueError, match="stream_id_field"):
        NencartaConfig(custom_config(workspace_tmp, stream_id_field=None))
    with pytest.raises(ValueError, match="forecasts come from GEOGLOWS or NWM"):
        NencartaConfig(custom_config(workspace_tmp, floodmap_mode="forecast"))
    with pytest.raises(ValueError, match="reanalysis_file"):
        NencartaConfig(custom_config(workspace_tmp, return_period_file=None))
    NencartaConfig(custom_config(workspace_tmp, return_period_file=None, reanalysis_file="flows.csv"))
    # A flow duration curve flow needs the curves
    with pytest.raises(ValueError, match="fdc_file"):
        NencartaConfig(custom_config(workspace_tmp, specified_bathyflow_field="p_exceed_50"))
    NencartaConfig(custom_config(workspace_tmp, specified_bathyflow_field="p_exceed_50", fdc_file="fdc.nc"))


def test_quotes_around_paths_are_dropped(workspace_tmp):
    flowline = workspace_tmp / "streams.parquet"
    config = NencartaConfig(base_config(workspace_tmp, flowline=f'"{flowline}"', return_period_file="'rp.nc'"))

    assert config.flowline == str(flowline)
    assert config.return_period_file == "rp.nc"


def test_workspace_uses_expected_output_paths(workspace_tmp):
    dem = workspace_tmp / "dems" / "fabdem.tif"
    dem.parent.mkdir(exist_ok=True)
    dem.touch()

    workspace = Workspace(NencartaConfig(base_config(workspace_tmp)), dem)

    assert workspace.output_dir == workspace_tmp / "out" / "example"
    assert workspace.FileName == "fabdem"
    assert workspace.assigned_dem == dem
    assert workspace.DEM_StrmShp.name == "GEOGLOWS_fabdem_StrmShp.gpkg"
    assert workspace.VDT_File_Bathy.name == "GEOGLOWS_fabdem_VDT_Database_Bathy.txt"


def test_workspace_bbox_names_synthetic_dem(workspace_tmp):
    config = base_config(
        workspace_tmp,
        dem_dir=None,
        source_dems=[str(workspace_tmp / "source.tif")],
        bbox=[-105.123456, 40.123456, -104.123456, 41.123456],
        buffer=True,
    )

    workspace = Workspace(NencartaConfig(config), None)

    assert workspace.FileName == "dem_-105.12346_40.12346_-104.12346_41.12346_buffered"
    assert workspace.assigned_dem.name.endswith(".tif")


def test_short_file_names_accept_a_dem_used_as_is(workspace_tmp):
    # Without a buffer or bbox the assigned DEM is the input DEM itself, so the two share a path by design
    dem = workspace_tmp / "dems" / "fabdem.tif"
    dem.parent.mkdir(exist_ok=True)
    dem.touch()

    workspace = Workspace(NencartaConfig(base_config(workspace_tmp, short_file_names=True)), dem)

    assert workspace.original_dem == workspace.assigned_dem == dem


def test_file_names_that_give_two_files_one_path_raise(workspace_tmp):
    dem = workspace_tmp / "dems" / "fabdem.tif"
    dem.parent.mkdir(exist_ok=True)
    dem.touch()
    config = base_config(workspace_tmp, short_file_names=True, file_names={"fixed": "dem_out", "filled": "dem_out"})

    with pytest.raises(ValueError, match="same path"):
        Workspace(NencartaConfig(config), dem)


def test_a_file_named_onto_the_input_dem_raises(workspace_tmp):
    # The DEM may share its path with the assigned DEM, but an output written over it would destroy the input
    dem = workspace_tmp / "dems" / "fabdem.tif"
    dem.parent.mkdir(exist_ok=True)
    dem.touch()
    config = base_config(workspace_tmp, short_file_names=True, file_names={"fixed": "fabdem"},
                         folder_paths={"DEM_Updated": str(workspace_tmp / "dems")})

    with pytest.raises(ValueError, match="same path"):
        Workspace(NencartaConfig(config), dem)
