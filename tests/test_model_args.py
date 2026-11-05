"""arc_args, curve2flood_args and fldpln_library_args: the models' own settings, which nencarta passes on."""
import pytest

from nencarta.core.configs import NencartaConfig
from nencarta.core.defaults import DEFAULT_ARC_ARGS, DEFAULT_CURVE2FLOOD_ARGS
from nencarta.core.inspect_config import inspect_config
from nencarta.core.model_args import get_arg, merge_args
from nencarta.core.model_config import ModelConfig
from nencarta.tasks.configs import define_arc_configs, define_mapper_bathymetry_config
from nencarta.workspace import Workspace


def make_config(workspace_tmp, **overrides) -> dict:
    config = {"name": "example", "dem": str(workspace_tmp / "dem.tif"), "flowline": str(workspace_tmp / "streams.gpkg"),
              "output_dir": str(workspace_tmp / "out")}
    config.update(overrides)
    return config


def test_the_given_settings_replace_nencarta_s_whatever_their_case_and_none_drops_one():
    merged = merge_args({"Degree_Manip": 6.1, "Gen_Dir_Dist": 10}, {"degree_manip": 20.1, "Gen_Dir_Dist": None,
                                                                    "k_decay": 3})
    assert merged == {"degree_manip": 20.1, "k_decay": 3}
    assert get_arg(merged, "DEGREE_MANIP") == 20.1 and get_arg(merged, "Low_Spot_Range", 2) == 2


def test_the_given_args_are_merged_over_nencarta_s(workspace_tmp):
    configs = NencartaConfig(make_config(workspace_tmp, arc_args={"Degree_Manip": 20.1, "Gen_Dir_Dist": None},
                                         curve2flood_args={"TW_MultFact": 6}))

    assert configs.arc_args == {**{k: v for k, v in DEFAULT_ARC_ARGS.items() if k != "Gen_Dir_Dist"},
                                "Degree_Manip": 20.1}
    assert configs.curve2flood_args == {**DEFAULT_CURVE2FLOOD_ARGS, "TW_MultFact": 6}


@pytest.mark.parametrize("key, value, where", [
    ("standard_step", True, "arc_args' Standard_Step"),
    ("fldpln_dh", 0.25, "fldpln_library_args' dh"),
    ("flood_bathy_mask", True, "curve2flood_args' Flood_Bathy_Mask"),
])
def test_an_option_that_is_now_a_model_s_says_where_it_went(workspace_tmp, key, value, where):
    with pytest.raises(ValueError, match=f"'{key}' is {where}"):
        NencartaConfig(make_config(workspace_tmp, **{key: value}))


def test_bathy_args_is_arc_args_with_its_old_meaning(workspace_tmp, caplog):
    """It replaced nencarta's set of its keys, but for VDT_Database_NumIterations and Bathy_Trap_H, and its
    TopWidthPlausibleLimit was the set-depth floods'."""
    configs = NencartaConfig(make_config(workspace_tmp, bathy_args={
        "Degree_Manip": 20.1, "TopWidthPlausibleLimit": 6000, "TW_MultFact": 6, "Make_Output_GPKG": False}))

    assert get_arg(configs.arc_args, "Degree_Manip") == 20.1
    assert get_arg(configs.arc_args, "Degree_Interval") is None  # ARC's default, as before
    assert get_arg(configs.arc_args, "VDT_Database_NumIterations") == 30
    assert get_arg(configs.arc_args, "Bathy_Trap_H") == 0.2
    # nencarta's other starting values, which were options of their own
    assert get_arg(configs.arc_args, "FindBanksBasedOnLandCover") is True
    assert get_arg(configs.arc_args, "TW_MultFact") is None
    assert configs.depth_mask_distance == 6000
    assert "'bathy_args' is now 'arc_args'" in caplog.text


def test_floodmap_args_is_curve2flood_args(workspace_tmp):
    configs = NencartaConfig(make_config(workspace_tmp, floodmap_args={"TW_MultFact": 2.0}))
    assert configs.curve2flood_args == {**DEFAULT_CURVE2FLOOD_ARGS, "TW_MultFact": 2.0}


def test_the_new_names_go_over_the_old_ones(workspace_tmp):
    """An old config's moved options go in the new dictionaries beside the old ones."""
    configs = NencartaConfig(make_config(workspace_tmp, bathy_args={"Degree_Manip": 20.1, "Gen_Dir_Dist": 5},
                                         arc_args={"Standard_Step": True, "gen_dir_dist": 8},
                                         floodmap_args={"TW_MultFact": 2.0},
                                         curve2flood_args={"Flood_Bathy_Mask": True}))

    assert get_arg(configs.arc_args, "Degree_Manip") == 20.1 and get_arg(configs.arc_args, "Gen_Dir_Dist") == 8
    assert configs.standard_step is True
    assert configs.curve2flood_args == {**DEFAULT_CURVE2FLOOD_ARGS, "TW_MultFact": 2.0, "Flood_Bathy_Mask": True}


@pytest.mark.parametrize("overrides, message", [
    ({"arc_args": {"Degree_Manipulation": 40}}, r"arc_args: .*'degree_manipulation' \(did you mean 'degree_manip'"),
    ({"arc_args": {"TW_MultFact": 1.5}}, r"arc_args: .*'tw_multfact'"),
    ({"arc_args": {"Gen_Dir_Dist": "ten"}}, "gen_dir_dist must be an integer"),
    ({"curve2flood_args": {"Standard_Step": True}}, r"curve2flood_args: .*'standard_step'"),
    ({"fldpln_library_args": {"spill_decayy": 0.1}}, r"no argument 'spill_decayy' \(did you mean 'spill_decay'"),
])
def test_settings_a_model_doesn_t_have_are_rejected_at_once(workspace_tmp, overrides, message):
    with pytest.raises(ValueError, match=message):
        NencartaConfig(make_config(workspace_tmp, **overrides))


@pytest.mark.parametrize("overrides, key", [
    ({"arc_args": {"DEM_File": "other.tif"}}, "DEM_File"),
    ({"arc_args": {"AROutBATHY": "bathy.tif"}}, "AROutBATHY"),
    ({"arc_args": {"Outlet_WSE_File": "outlets.parquet"}}, "Outlet_WSE_File"),
    ({"curve2flood_args": {"OutFLD": "flood.tif"}}, "OutFLD"),
    ({"curve2flood_args": {"FloodSpreader_SpecifyDepth": 0.3}}, "FloodSpreader_SpecifyDepth"),
    ({"curve2flood_args": {"Bathy_Use_Banks": True}}, "Bathy_Use_Banks"),
    ({"fldpln_library_args": {"library_file": "lib.parquet"}}, "library_file"),
])
def test_the_files_and_settings_nencarta_sets_can_t_be_given(workspace_tmp, overrides, key):
    with pytest.raises(ValueError, match=f"can't give {key}: nencarta"):
        NencartaConfig(make_config(workspace_tmp, **overrides))


def test_the_library_can_t_be_built_in_parallel_beside_the_dems(workspace_tmp):
    with pytest.raises(ValueError, match="'parallel'"):
        NencartaConfig(make_config(workspace_tmp, parallel=True, fldpln_library_args={"parallel": True}))


def test_curve2flood_burns_the_bathymetry_in_from_a_file_of_its_own(workspace_tmp):
    from arc.config import Configs
    from curve2flood.config import Config

    configs = NencartaConfig(make_config(workspace_tmp, arc_args={"Bathy_Use_Banks": True}, land_watervalue=11))
    workspace = Workspace(configs, workspace_tmp / "dem.tif")

    bathy_config = define_mapper_bathymetry_config(workspace)

    curve2flood = Config.from_file(bathy_config)
    assert curve2flood.makes_bathymetry and not curve2flood.needs_flood_map
    assert curve2flood.bathy_out_file == str(workspace.ARC_BathyFile)
    assert curve2flood.fsoutbathy == str(workspace.FS_BathyFile)
    # As ARC estimated it
    assert curve2flood.bathy_use_banks is True
    arc = Configs.from_file(define_arc_configs(workspace))
    assert arc.bathy_use_banks is True and arc.bathy_out_file == str(workspace.ARC_BathyFile)
    # ARC finds the banks at the land cover's water, nencarta's land_watervalue
    assert arc.lc_water_value == 11

    model_config = ModelConfig(None, [], [], workspace.mapper, bathy_mapper_config=bathy_config)
    assert not model_config.burned_dem_exists
    workspace.FS_BathyFile.parent.mkdir(parents=True, exist_ok=True)
    workspace.FS_BathyFile.touch()
    assert model_config.burned_dem_exists


def test_without_bathymetry_curve2flood_has_nothing_to_burn_in(workspace_tmp):
    configs = NencartaConfig(make_config(workspace_tmp, disable_bathymetry=True))
    assert define_mapper_bathymetry_config(Workspace(configs, workspace_tmp / "dem.tif")) is None
    assert inspect_config(define_arc_configs(Workspace(configs, workspace_tmp / "dem.tif")), "BATHY_Out_File") is None
