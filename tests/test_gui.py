"""The GUI has a field for every config key, and a fresh form sends the defaults."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PyQt5.QtCore")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from nencarta import gui_app
from nencarta.core.configs import NencartaConfig
from nencarta.core.defaults import CONFIG_OPTIONS, DEFAULT_CONFIG


@pytest.fixture
def window(workspace_tmp, monkeypatch):
    # A settings file of its own, so the test neither reads nor changes the user's saved settings
    monkeypatch.setattr(gui_app, "settings", QtCore.QSettings(str(workspace_tmp / "gui.ini"), QtCore.QSettings.IniFormat))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = gui_app.FloodSimulationGUI()
    yield window
    window.close()


def fill_required(window, workspace_tmp):
    window.input_fields["output_dir"].line_edit.setText(str(workspace_tmp / "out"))
    window.input_fields["dem"].line_edit.setText(str(workspace_tmp / "dem.tif"))
    window.input_fields["flowline"].line_edit.setText(str(workspace_tmp / "streams.gpkg"))


def set_choice(window, name, value):
    window.choices[name].set_value(value)
    window._refresh_visibility()


def set_combo(window, key, text):
    window.input_fields[key].setCurrentText(text)
    window._refresh_visibility()


def check(window, key, checked=True):
    window.input_fields[key].setChecked(checked)
    window._refresh_visibility()


def test_every_config_key_has_a_field(window):
    keys = {"name" if key == "watershed_name" else key for key in window.input_fields}

    assert keys == set(CONFIG_OPTIONS)


def test_a_fresh_form_sends_the_defaults(window, workspace_tmp, caplog):
    window.input_fields["output_dir"].line_edit.setText(str(workspace_tmp / "out"))
    window.input_fields["dem"].line_edit.setText(str(workspace_tmp / "dem.tif"))
    window.input_fields["flowline"].line_edit.setText(str(workspace_tmp / "streams.gpkg"))

    params = window._get_params()

    for key, value in params.items():
        if key not in ("name", "output_dir", "dem", "flowline"):
            assert value == DEFAULT_CONFIG[key], key
    NencartaConfig(params)
    assert "unknown keys" not in caplog.text


def test_the_tabs_are_in_order(window):
    tabs = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    assert tabs == ["Inputs", "Preprocessing", "ARC", "Mapping", "FIST", "Go Consequences", "Run"]


def test_labels_name_the_setting_behind_them(window):
    assert set(gui_app.UNDERLYING_NAMES) <= set(window.rows)
    assert window.arg_fields[("arc_args", "Bathy_Use_Banks")][0].text().endswith("(Bathy_Use_Banks)")
    assert window.rows["arc_args.Gen_Dir_Dist"][0].text().endswith("(Gen_Dir_Dist)")
    assert window.choices["use_specified_depth_for_bathy_mask"].buttons["water"].text().endswith("(BathyWaterMask)")
    assert window.input_fields["make_vdt"].toolTip().startswith("make_vdt: ")


def test_only_the_chosen_dem_source_is_sent(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    window.input_fields["dem_dir"].line_edit.setText(str(workspace_tmp))
    window.input_fields["bbox"].setText("1, 2, 3, 4")
    params = window._get_params()
    assert "dem" in params and "dem_dir" not in params and "bbox" not in params

    set_choice(window, "dem_mode", "folder")
    params = window._get_params()
    assert "dem_dir" in params and "dem" not in params

    set_choice(window, "dem_mode", "bbox")
    params = window._get_params()
    assert params["bbox"] == [1.0, 2.0, 3.0, 4.0] and "dem" not in params
    assert "source_dems" not in window.hidden and "buffer_distance" in window.hidden
    check(window, "buffer")
    assert "buffer_distance" not in window.hidden


def test_fields_follow_the_streamflow_source_and_the_mapper(window):
    assert {"nwm_api_key", "return_period_file"} <= window.hidden
    set_combo(window, "streamflow_source", "NWM Short Range")
    assert "nwm_api_key" not in window.hidden and "include_fdc" in window.hidden
    set_combo(window, "streamflow_source", "Custom (my own flows)")
    assert "return_period_file" not in window.hidden and "nwm_api_key" in window.hidden

    fldpln = {"use_parquet", "fldpln_library_args.solver", "fldpln_library_args.spill_decay",
              "fldpln_library_args.dh", "fldpln_library_args.iterative_spill", "fldpln_library_args"}
    assert fldpln <= window.hidden and "curve2flood_args.TW_MultFact" not in window.hidden
    set_combo(window, "mapper", "Curve2Flood-FLDPLNpy")
    assert not fldpln & window.hidden and "curve2flood_args.TW_MultFact" in window.hidden
    # The depth step and keep-spilling are the (default) iterative solver's, the cell limit the exact one's
    solver = window.arg_fields[("fldpln_library_args", "solver")][0]
    assert solver.currentText() == "iterative"
    assert "fldpln_library_args.max_cells_per_segment" in window.hidden
    solver.setCurrentText("exact")
    window._refresh_visibility()
    assert {"fldpln_library_args.dh", "fldpln_library_args.iterative_spill"} <= window.hidden
    assert "fldpln_library_args.max_cells_per_segment" not in window.hidden


def test_a_low_spot_range_with_fldpln_is_warned_about(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    set_combo(window, "mapper", "Curve2Flood-FLDPLNpy")
    check(window, "move_stream_network_to_thalweg")
    window.arg_fields[("arc_args", "Low_Spot_Range")][0].setText("2")
    window._refresh_visibility()
    assert "low_spot_warning" not in window.hidden
    assert any("Low_Spot_Range" in warning for warning in window._validate_params(window._get_params()))


def test_a_custom_source_is_sent_with_its_files(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    set_combo(window, "streamflow_source", "Custom (my own flows)")
    set_combo(window, "floodmap_mode", "User flow files")
    window.input_fields["user_flow_files"].add_value(str(workspace_tmp / "flows.csv"))
    window.input_fields["stream_id_field"].setText("ID")
    window.input_fields["downstream_id_field"].setText("DS_ID")
    window.input_fields["return_period_file"].line_edit.setText(str(workspace_tmp / "rp.nc"))
    params = window._get_params()

    assert params["streamflow_source"] == "CUSTOM" and params["floodmap_mode"] == "user"
    window._validate_params(params)


def test_quotes_around_pasted_paths_are_dropped(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    window.input_fields["dem"].line_edit.setText(f'"{workspace_tmp / "dem.tif"}"')
    picker = window.input_fields["source_dems"]
    picker.paste_edit.setText('"C:\\a, b.tif" "C:\\c.tif"')
    picker._add_pasted()

    assert window._get_params()["dem"] == str(workspace_tmp / "dem.tif")
    assert picker.values() == ["C:\\a, b.tif", "C:\\c.tif"]


def test_arc_arguments_come_from_their_fields_and_the_table(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    window.arg_fields[("arc_args", "Degree_Manip")][0].setText("12.5")
    window.input_fields["arc_args"].add_row("k_decay", "4")
    params = window._get_params()
    assert params["arc_args"]["Degree_Manip"] == 12.5 and params["arc_args"]["k_decay"] == 4

    # A hidden argument is left out: end_points measures no slope over Gen_Slope_Dist
    window.arg_fields[("arc_args", "Stream_Slope_Method")][0].setCurrentText("end_points")
    window._refresh_visibility()
    params = window._get_params()
    assert "Gen_Slope_Dist" not in params["arc_args"] and "Slope_Low_Percentile" not in params["arc_args"]


def test_local_fit_measures_its_slopes_over_gen_slope_dist_without_the_percentiles(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    method = window.arg_fields[("arc_args", "Stream_Slope_Method")][0]
    method.setCurrentText("local_fit")
    window._refresh_visibility()
    params = window._get_params()
    assert params["arc_args"]["Stream_Slope_Method"] == "local_fit"
    assert "Gen_Slope_Dist" in params["arc_args"]
    assert "Slope_Low_Percentile" not in params["arc_args"] and "Slope_High_Percentile" not in params["arc_args"]


@pytest.mark.parametrize("switch, settings, values", [
    ("Valley_Sections", ("Valley_Dir_Widths", "Valley_Dir_Dist"), ("12", "80")),
    ("Drop_Edge_Sections", ("Edge_Margin",), ("90",)),
])
def test_an_arc_setting_s_own_settings_are_sent_only_with_it(window, workspace_tmp, switch, settings, values):
    fill_required(window, workspace_tmp)
    assert window.arg_fields[("arc_args", switch)][0].text().endswith(f"({switch})")
    for setting in settings:
        assert window.rows[f"arc_args.{setting}"][0].text().endswith(f"({setting})")

    # Off, ARC's default: none of them sent
    params = window._get_params()
    assert not {switch, *settings} & set(params.get("arc_args", {}))

    window.arg_fields[("arc_args", switch)][0].setChecked(True)
    for setting, value in zip(settings, values):
        window.arg_fields[("arc_args", setting)][0].setText(value)
    window._refresh_visibility()
    params = window._get_params()
    assert params["arc_args"][switch] is True
    assert [params["arc_args"][setting] for setting in settings] == [float(value) for value in values]
    NencartaConfig(params)


def test_the_water_mask_choice_and_its_depths(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    set_choice(window, "use_specified_depth_for_bathy_mask", "depth")
    depths = window.input_fields["specify_depths_for_bathy_mask"]
    depths.bathymetry_edit.setText("0.3")
    depths.cleaner_edit.setText("0.2")
    assert window._get_params()["specify_depths_for_bathy_mask"] == [0.3]
    check(window, "clean_dem")
    assert window._get_params()["specify_depths_for_bathy_mask"] == [0.2, 0.3]

    set_choice(window, "use_specified_depth_for_bathy_mask", "water")
    params = window._get_params()
    assert params["use_specified_depth_for_bathy_mask"] is False and "specify_depths_for_bathy_mask" not in params


def test_the_form_is_remembered(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    set_choice(window, "dem_mode", "folder")
    window.input_fields["dem_dir"].line_edit.setText(str(workspace_tmp))
    check(window, "disable_bathymetry", False)  # ticked means carve bathymetry
    window.arg_fields[("arc_args", "Low_Spot_Range")][0].setText("0")
    window.input_fields["folder_paths"].add_row("default", "{name}/{dem}/{folder}")
    window.save_settings()

    again = gui_app.FloodSimulationGUI()
    params = again._get_params()
    again.close()
    assert again.choices["dem_mode"].value() == "folder" and params["dem_dir"] == str(workspace_tmp)
    assert params["disable_bathymetry"] is True and params["arc_args"]["Low_Spot_Range"] == 0
    assert params["folder_paths"] == {"default": "{name}/{dem}/{folder}"}


def test_the_output_paths_are_previewed(window, workspace_tmp):
    fill_required(window, workspace_tmp)
    window.input_fields["folder_paths"].add_row("default", "{name}/{dem}/{folder}")
    window._refresh_names_preview()
    assert str(workspace_tmp / "out" / "example" / "dem" / "VDT") in window.names_preview.toPlainText()


def test_arguments_no_longer_read_are_dropped_from_the_remembered_tables(workspace_tmp, monkeypatch):
    saved = QtCore.QSettings(str(workspace_tmp / "old.ini"), QtCore.QSettings.IniFormat)
    saved.setValue("arc_args", '{"Str_Limit_Val": 1, "FS_ADJUST_FLOW_BY_FRACTION": 1.0, "X_Section_Dist": 5000, '
                               '"k_decay": 4}')
    saved.setValue("curve2flood_args", '{"FS_ADJUST_FLOW_BY_FRACTION": 1.0}')
    monkeypatch.setattr(gui_app, "settings", saved)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = gui_app.FloodSimulationGUI()
    fill_required(window, workspace_tmp)
    params = window._get_params()
    window.close()

    assert params["arc_args"]["k_decay"] == 4
    assert not gui_app.RETIRED_ARGS & (set(params["arc_args"]) | set(params["curve2flood_args"]))


class LogSignal:
    """Sends what the worker's stream writes straight to the window's log."""
    def __init__(self, window):
        self.window = window

    def emit(self, message):
        self.window.log_message(message)


def log_lines(window):
    return window.log_text.toPlainText().splitlines()


def test_a_progress_bar_is_rewritten_on_its_own_line(window):
    tqdm = pytest.importorskip("tqdm").tqdm
    window.log_text.clear()
    stream = gui_app.QtLogStream(LogSignal(window))
    for k in tqdm(range(50), file=stream, mininterval=0, desc="Cross sections"):
        if k == 20:
            window.log_message("[INFO] logged while the bar runs")
    for _ in tqdm(range(5), file=stream, mininterval=0):  # ARC's bars have no name
        pass
    for _ in tqdm(range(5), file=stream, mininterval=0):
        pass

    lines = log_lines(window)
    assert len(lines) == 4, lines
    assert lines[0].startswith("Cross sections: 100%|") and lines[1] == "[INFO] logged while the bar runs"
    assert all(line.startswith("100%|") for line in lines[2:])


def test_a_bar_without_a_total_and_a_rewritten_line_end_with_their_lines(window):
    tqdm = pytest.importorskip("tqdm").tqdm
    window.log_text.clear()
    stream = gui_app.QtLogStream(LogSignal(window))
    for _ in range(2):
        for _ in tqdm(iter(range(7)), file=stream, mininterval=0, desc="Reading"):
            pass
    for percent in (10, 50, 90):
        stream.write(f"\rdone {percent}%")
    stream.write("\n")
    stream.write("next line\n")

    lines = log_lines(window)
    assert [line.split(":")[0] for line in lines[:2]] == ["Reading", "Reading"] and "7it [" in lines[0]
    assert lines[2:] == ["done 90%", "next line"]


def test_nested_bars_keep_a_line_each(window):
    tqdm = pytest.importorskip("tqdm").tqdm
    window.log_text.clear()
    stream = gui_app.QtLogStream(LogSignal(window))
    for _ in tqdm(range(3), file=stream, mininterval=0, desc="Reaches", position=0):
        for _ in tqdm(range(4), file=stream, mininterval=0, desc="Sections", position=1, leave=False):
            pass

    lines = log_lines(window)
    assert sum(line.startswith("Reaches:") for line in lines) == 1, lines
    assert sum(line.startswith("Sections:") for line in lines) <= 3, lines
    assert not any("\x1b" in line for line in lines)


TWO_RUNS = '''
import logging
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt5 import QtCore, QtWidgets

import nencarta.main  # noqa: F401
from nencarta import gui_app

out = sys.argv[1]
gui_app.settings = QtCore.QSettings(os.path.join(out, "gui.ini"), QtCore.QSettings.IniFormat)
app = QtWidgets.QApplication([])
window = gui_app.FloodSimulationGUI()
window.input_fields["output_dir"].line_edit.setText(os.path.join(out, "out"))
window.input_fields["dem"].line_edit.setText(os.path.join(out, "dem.tif"))
window.input_fields["flowline"].line_edit.setText(os.path.join(out, "streams.gpkg"))
library = logging.getLogger("imported_during_the_first_run")


def first_run(params):
    # What ARC's and Curve2Flood's _log modules do when the first run imports them
    library.addHandler(logging.StreamHandler(sys.stdout))


def second_run(params):
    library.warning("written to the first run's stream")


for run in (first_run, second_run):
    sys.modules["nencarta.main"].process_watershed = run  # the package's main is a function of that name
    window.start_simulation()
    window.worker_thread.wait()
    app.processEvents()
print("LOG:", window.log_text.toPlainText())
'''


def test_a_second_run_can_write_to_the_stream_a_library_kept_from_the_first(workspace_tmp):
    """
    ARC and Curve2Flood give their loggers the sys.stdout they are first imported under, which in the GUI is the
    first run's stream; each run used to have a stream of its own, sending to its own worker, which the next run
    replaces. ARC's first warning in the second run then crashed the GUI. In a process of its own, since that crash
    kills the process.
    """
    import subprocess
    import sys

    script = workspace_tmp / "two_runs.py"
    script.write_text(TWO_RUNS)
    done = subprocess.run([sys.executable, str(script), str(workspace_tmp)], capture_output=True, encoding="utf-8",
                          errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=300)

    assert done.returncode == 0, done.stderr[-2000:]
    assert "written to the first run's stream" in done.stdout.split("LOG:", 1)[-1]
