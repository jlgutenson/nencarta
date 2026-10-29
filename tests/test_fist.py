from pathlib import Path

from nencarta.core.configs import NencartaConfig
from nencarta.tasks.fist import get_fist_inputs
from nencarta.workspace import Workspace


def make_workspace(workspace_tmp, columns=("flow_a", "flow_b"), flows=None, **overrides):
    dem = workspace_tmp / "dems" / "fabdem.tif"
    dem.parent.mkdir(exist_ok=True)
    dem.touch()
    flow_file = workspace_tmp / "flows.csv"
    flow_file.write_text(f"rivid,{','.join(columns)}\n1,{','.join(flows or ['10.5'] * len(columns))}\n")
    config = {"name": "example", "dem": str(dem), "flowline": str(workspace_tmp / "streams.gpkg"),
              "output_dir": str(workspace_tmp / "out"), "floodmap_mode": "user", "user_flow_files": [str(flow_file)],
              "make_fist_inputs": True}
    config.update(overrides)
    return Workspace(NencartaConfig(config), dem), flow_file


def test_fist_inputs_are_made_from_the_curve_file_and_the_dem(workspace_tmp):
    workspace, flow_file = make_workspace(workspace_tmp, burn_streams=True)

    tasks = get_fist_inputs(workspace, flow_file)

    assert [task[0] for task in tasks] == [str(workspace.Curve_File_Bathy)] * 2
    assert [task[1] for task in tasks] == [str(workspace.fixed_dem)] * 2


def test_every_flow_column_gets_fist_inputs_though_its_flows_are_whole_numbers(workspace_tmp):
    workspace, flow_file = make_workspace(workspace_tmp, columns=("whole", "decimal"), flows=["100", "10.5"])

    tasks = get_fist_inputs(workspace, flow_file)

    assert [list(task[10].columns) for task in tasks] == [["rivid", "whole"], ["rivid", "decimal"]]


def test_fist_inputs_are_only_remade_when_missing(workspace_tmp):
    workspace, flow_file = make_workspace(workspace_tmp)
    for task in get_fist_inputs(workspace, flow_file):
        Path(task[2]).parent.mkdir(parents=True, exist_ok=True)
        Path(task[2]).touch()

    assert get_fist_inputs(workspace, flow_file) == []
    workspace.configs.overwrite = True
    assert len(get_fist_inputs(workspace, flow_file)) == 2


def test_each_new_forecast_gets_fist_inputs_though_the_seed_file_exists(workspace_tmp):
    workspace, flow_file = make_workspace(workspace_tmp, columns=("median", "min", "max"), floodmap_mode="forecast")
    workspace.configs.forecastdate = "20250807"
    seed = workspace.file_path(workspace.FIST_Folder, 'Seed', 'parquet')
    seed.parent.mkdir(parents=True)
    seed.touch()

    tasks = get_fist_inputs(workspace, flow_file)

    assert [Path(task[2]).name for task in tasks] == [f"fabdem_20250807_{column}.geojson" for column in ("median", "min", "max")]
    assert all(task[7] == str(seed) for task in tasks) and seed.exists()


def test_no_fist_inputs_without_a_curve_file(workspace_tmp):
    workspace, flow_file = make_workspace(workspace_tmp, make_curvefile=False)

    assert get_fist_inputs(workspace, flow_file) == []


def test_return_period_runs_make_no_fist_inputs(workspace_tmp):
    workspace, _ = make_workspace(workspace_tmp, floodmap_mode="return_period", return_periods=[2])

    assert get_fist_inputs(workspace, workspace.return_period_flow_file(2)) == []
