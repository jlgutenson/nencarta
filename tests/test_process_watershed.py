import importlib
import json
from pathlib import Path

import pytest

main = importlib.import_module("nencarta.main")


def base_config(workspace_tmp: Path, **overrides) -> dict:
    dem_dir = workspace_tmp / "dems"
    dem_dir.mkdir(exist_ok=True)
    config = {
        "name": "entrypoint",
        "flowline": str(workspace_tmp / "streams.parquet"),
        "dem_dir": str(dem_dir),
        "output_dir": str(workspace_tmp / "out"),
        "mapper": "Curve2Flood-Kernel Weighted",
        "streamflow_source": "GEOGLOWS",
        "use_specified_depth_for_bathy_mask": True,
        "specify_depths_for_bathy_mask": [0.1],
        "clean_dem": False,
        "make_fist_inputs": False,
        "profile": False,
    }
    config.update(overrides)
    return config


def test_process_watershed_builds_workspace_for_each_matching_dem(workspace_tmp, monkeypatch):
    dem_dir = workspace_tmp / "dems"
    dem_dir.mkdir()
    (dem_dir / "a.tif").touch()
    (dem_dir / "b.tif").touch()
    (dem_dir / "ignore.txt").touch()
    captured = []

    monkeypatch.setattr(main, "run_pipeline", lambda workspaces: captured.extend(workspaces))

    main.process_watershed(base_config(workspace_tmp, dem_filter="*.tif"))

    assert [workspace.FileName for workspace in captured] == ["a", "b"]


def test_process_watershed_uses_synthetic_dem_when_bbox_and_source_dems_exist(workspace_tmp, monkeypatch):
    captured = []
    monkeypatch.setattr(main, "run_pipeline", lambda workspaces: captured.extend(workspaces))

    main.process_watershed(
        base_config(
            workspace_tmp,
            dem_dir=None,
            source_dems=[str(workspace_tmp / "source.tif")],
            bbox=[-105, 40, -104, 41],
        )
    )

    assert len(captured) == 1
    assert captured[0].FileName == "dem_-105.00000_40.00000_-104.00000_41.00000"


def test_process_watershed_returns_without_pipeline_when_no_dem_source(workspace_tmp, monkeypatch):
    called = False

    def fail_if_called(workspaces):
        nonlocal called
        called = True

    monkeypatch.setattr(main, "run_pipeline", fail_if_called)

    main.process_watershed(base_config(workspace_tmp, dem_dir=None, source_dems=[]))

    assert called is False


def test_process_json_input_arguments_override_the_file(workspace_tmp, monkeypatch):
    json_file = workspace_tmp / "input.json"
    watershed = base_config(workspace_tmp)
    json_file.write_text(json.dumps({"parallel": True, "num_workers": 2, "watersheds": [watershed]}), encoding="utf-8")
    captured = []

    monkeypatch.setattr(main, "process_many_watersheds", lambda input_dicts: captured.extend(input_dicts))

    main.process_json_input(json_file, parallel=False, num_workers=4)

    assert captured == [dict(watershed, parallel=False, num_workers=4)]


def test_process_json_input_calls_process_many(workspace_tmp, monkeypatch):
    json_file = workspace_tmp / "input.json"
    watershed = base_config(workspace_tmp)
    json_file.write_text(json.dumps({"watersheds": [watershed]}), encoding="utf-8")
    captured = []

    monkeypatch.setattr(main, "process_many_watersheds", lambda input_dicts: captured.extend(input_dicts))

    main.process_json_input(json_file, parallel=False)

    assert captured == [dict(watershed, parallel=False)]


def test_process_json_input_applies_top_level_run_settings(workspace_tmp, monkeypatch):
    json_file = workspace_tmp / "input.json"
    watersheds = [
        base_config(workspace_tmp, name="one"),
        base_config(workspace_tmp, name="two"),
    ]
    json_file.write_text(json.dumps({"parallel": True, "num_workers": 2, "watersheds": watersheds}), encoding="utf-8")
    captured = []

    monkeypatch.setattr(main, "process_many_watersheds", lambda input_dicts: captured.extend(input_dicts))

    main.process_json_input(json_file)

    assert captured == [
        dict(watersheds[0], parallel=True, num_workers=2),
        dict(watersheds[1], parallel=True, num_workers=2),
    ]


def run_cli(monkeypatch, *argv) -> dict:
    """The watershed dictionary `flood-mapping cli <argv>` passes to process_watershed."""
    captured = []
    monkeypatch.setattr(main, "process_watershed", lambda input_dict: captured.append(input_dict))
    main.process_cli_arguments(main.build_parser().parse_args(["cli", *argv]))
    return captured[0]


def test_cli_passes_only_the_options_given(workspace_tmp, monkeypatch):
    watershed = run_cli(
        monkeypatch, "--name", "cli_entrypoint", "--output_dir", str(workspace_tmp / "out"),
        "--flowline", str(workspace_tmp / "streams.parquet"), "--dem_dir", str(workspace_tmp / "dems"), "--overwrite",
    )

    assert watershed == {
        "name": "cli_entrypoint",
        "flowline": str(workspace_tmp / "streams.parquet"),
        "dem_dir": str(workspace_tmp / "dems"),
        "output_dir": str(workspace_tmp / "out"),
        "overwrite": True,
    }


def test_cli_options_take_each_kind_of_value(monkeypatch):
    watershed = run_cli(
        monkeypatch, "--name", "x", "--output_dir", "out", "--dem", "dem.tif", "--flowline", "streams.gpkg",
        "--no-relocate_misplaced_streams", "--return_periods", "2", "100", "--bbox", "-105", "40", "-104", "41",
        "--depth_mask_distance", "12.5", "--age_of_forecast_days", "3", "--arc_args", '{"Gen_Slope_Dist": 20}',
        "--mapper", "Curve2Flood-FLDPLNpy",
    )

    assert watershed["relocate_misplaced_streams"] is False
    assert watershed["return_periods"] == [2, 100]
    assert watershed["bbox"] == [-105.0, 40.0, -104.0, 41.0]
    assert watershed["depth_mask_distance"] == 12.5
    assert watershed["age_of_forecast_days"] == 3
    assert watershed["arc_args"] == {"Gen_Slope_Dist": 20}
    assert watershed["mapper"] == "Curve2Flood-FLDPLNpy"


def test_cli_requires_a_name_and_an_output_folder(capsys):
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["cli", "--dem", "dem.tif"])

    assert "--name" in capsys.readouterr().err
