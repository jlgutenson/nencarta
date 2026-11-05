"""
The NenCarta GUI: a form with a field for every config key (see core/defaults.py) that runs process_watershed
and shows its log. Field values are remembered between sessions.
"""
# built-in imports
import dataclasses
import inspect
import io
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path

# third-party imports
from PyQt5.QtCore import QObject, QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPalette, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# local imports
from nencarta.core.configs import NencartaConfig, strip_quotes
from nencarta.core.defaults import (
    CONFIG_OPTIONS,
    DEFAULT_ARC_ARGS,
    DEFAULT_CONFIG,
    DEFAULT_CURVE2FLOOD_ARGS,
    DEFAULT_FLDPLN_LIBRARY_ARGS,
    FLOW_FIELD_OPTIONS,
    VALID_RETURN_PERIODS,
)
from nencarta.core.enumerations import FloodMapMode, Mapper, StreamflowSource
from nencarta.logger import LOG
from nencarta.workspace import FILE_BLURBS, WORKSPACE_FOLDERS, Workspace

settings = QSettings("NenCarta", "FloodSimulationGUI")

STREAMFLOW_SOURCE_LABELS = {
    "GEOGLOWS V2": str(StreamflowSource.GEOGLOWS_V2),
    "NWM Short Range": str(StreamflowSource.NWM_SHORT_RANGE),
    "NWM Medium Range": str(StreamflowSource.NWM_MEDIUM_RANGE),
    "NWM Long Range": str(StreamflowSource.NWM_LONG_RANGE),
    "Custom (my own flows)": str(StreamflowSource.CUSTOM),
}
FLOODMAP_MODE_LABELS = {
    "Forecast": FloodMapMode.FORECAST.value,
    "User flow files": FloodMapMode.USER.value,
    "Return periods": FloodMapMode.RETURN_PERIOD.value,
}

# Choices the form makes that aren't config keys themselves: they pick which of the keys it sends
DEM_MODES = {"file": "A DEM file", "folder": "A folder of DEMs", "bbox": "Cut from source DEMs to a bounding box"}
FLOWLINE_MODES = {"file": "One stream network file", "many": "Search several stream network files"}
WATER_MASK_MODES = {"depth": "The streams flooded to set depths",
                    "water": "The streams and the water beside them at the river's level"}
MANNINGS_MODES = {"default": "NenCarta's table, for ESA WorldCover", "file": "My own table"}

SLOPE_METHODS = ("local_average_corrected", "local_average", "reach_average", "end_points", "local_fit")

# The ARC and Curve2Flood settings and FLDPLN library arguments with fields of their own; the rest of arc_args,
# curve2flood_args and fldpln_library_args go in tables
ARC_ARG_FIELDS = ("Gen_Dir_Dist", "Degree_Manip", "Degree_Interval", "Low_Spot_Range", "Bathy_Trap_H",
                  "FindBanksBasedOnLandCover", "Bathy_Use_Banks", "coefficient_depth", "exponent_depth",
                  "coefficient_width", "exponent_width", "Stream_Slope_Method", "Gen_Slope_Dist",
                  "Slope_Low_Percentile", "Slope_High_Percentile", "Standard_Step", "Valley_Sections",
                  "Valley_Dir_Widths", "Valley_Dir_Dist", "Drop_Edge_Sections", "Edge_Margin",
                  "Reach_Average_Curve_File", "VDT_Database_NumIterations")
CURVE2FLOOD_ARG_FIELDS = ("FLDPLN_Min_Flood_Depth", "Flood_WaterLC_and_STRM_Cells", "Flood_Bathy_Mask", "TW_MultFact",
                          "TopWidthPlausibleLimit", "Make_Output_GPKG")
FLDPLN_LIBRARY_ARG_FIELDS = ("solver", "dh", "iterative_spill", "parallel", "fldmn", "fldmx", "spill_decay",
                             "max_cells_per_segment")
ARG_FIELDS = {"arc_args": ARC_ARG_FIELDS, "curve2flood_args": CURVE2FLOOD_ARG_FIELDS,
              "fldpln_library_args": FLDPLN_LIBRARY_ARG_FIELDS}
ARG_DEFAULTS = {"arc_args": DEFAULT_ARC_ARGS, "curve2flood_args": DEFAULT_CURVE2FLOOD_ARGS,
                "fldpln_library_args": DEFAULT_FLDPLN_LIBRARY_ARGS}
# Arguments neither ARC nor Curve2Flood reads any more, dropped from the tables the form remembered. ARC's cross
# sections reach as far as their water needs, so X_Section_Dist sets nothing.
RETIRED_ARGS = {"Str_Limit_Val", "FS_ADJUST_FLOW_BY_FRACTION", "X_Section_Dist"}

# The tags a folder path template can use (see Workspace.folder_path)
FOLDER_TAGS = ("{output_dir}", "{name}", "{dem}", "{folder}")

# The setting behind each field whose label doesn't name it plainly, shown after the label: ARC's or Curve2Flood's
# input file key where the field sets one, otherwise nencarta's config key. Keyed by row name (see _place).
UNDERLYING_NAMES = {
    # Inputs
    "use_warning_flags_to_download_dem": "use_warning_flags_to_download_dem",
    "streams_as_parquet": "streams_as_parquet",
    "exclude": "exclude",
    # Preprocessing
    "specified_bathyflow_field": "Flow_File_BF",
    "specified_highflow_field": "Flow_File_QMax",
    "reanalysis_file": "reanalysis_file",
    "include_fdc": "include_fdc",
    "raise_errors_if_river_ids_missing": "raise_errors_if_river_ids_missing",
    "reanalysis_storage_options": "reanalysis_storage_options",
    "floodmap_mode": "floodmap_mode",
    "forensic_forecast_date": "forensic_forecast_date",
    "forensic_forecast_hour": "forensic_forecast_hour",
    "remove_old_forecast_files": "remove_old_forecast_files",
    "age_of_forecast_days": "age_of_forecast_days",
    "move_stream_network_to_thalweg": "move_stream_network_to_thalweg",
    "relocate_misplaced_streams": "relocate_misplaced_streams",
    "minimize_output_files": "minimize_output_files",
    "new_strm_threshold_km2": "new_strm_threshold_km2",
    "use_dem_derived_channel_mask": "use_dem_derived_channel_mask",
    "dem_flattens_rivers": "dem_flattens_rivers",
    "drop_multilinestrings": "drop_multilinestrings",
    "q_baseflow_threshold": "q_baseflow_threshold",
    "land_cover_cache": "land_cover_cache",
    "land_watervalue": "LAND_WaterValue",
    "use_specified_depth_for_bathy_mask": "use_specified_depth_for_bathy_mask",
    "specify_depths_for_bathy_mask": "specify_depths_for_bathy_mask",
    "depth_mask_distance": "depth_mask_distance",
    # ARC
    "arc_args.Gen_Dir_Dist": "Gen_Dir_Dist",
    "arc_args.Degree_Manip": "Degree_Manip",
    "arc_args.Degree_Interval": "Degree_Interval",
    "arc_args.Low_Spot_Range": "Low_Spot_Range",
    "disable_bathymetry": "disable_bathymetry off",
    "arc_args.Bathy_Trap_H": "Bathy_Trap_H",
    "arc_args.FindBanksBasedOnLandCover": "FindBanksBasedOnLandCover",
    "arc_args.Bathy_Use_Banks": "Bathy_Use_Banks",
    "arc_args.coefficient_depth": "coefficient_depth",
    "arc_args.exponent_depth": "exponent_depth",
    "arc_args.coefficient_width": "coefficient_width",
    "arc_args.exponent_width": "exponent_width",
    "arc_args.Stream_Slope_Method": "Stream_Slope_Method",
    "arc_args.Gen_Slope_Dist": "Gen_Slope_Dist",
    "arc_args.Slope_Low_Percentile": "Slope_Low_Percentile",
    "arc_args.Slope_High_Percentile": "Slope_High_Percentile",
    "mannings_mode": "LU_Manning_n",
    "mannings_text_file": "mannings_text_file",
    "arc_args.Standard_Step": "Standard_Step",
    "arc_args.Valley_Sections": "Valley_Sections",
    "arc_args.Valley_Dir_Widths": "Valley_Dir_Widths",
    "arc_args.Valley_Dir_Dist": "Valley_Dir_Dist",
    "arc_args.Drop_Edge_Sections": "Drop_Edge_Sections",
    "arc_args.Edge_Margin": "Edge_Margin",
    "arc_args": "arc_args",
    "make_curvefile": "Print_Curve_File",
    "arc_args.Reach_Average_Curve_File": "Reach_Average_Curve_File",
    "make_vdt": "Print_VDT_Database",
    "arc_args.VDT_Database_NumIterations": "VDT_Database_NumIterations",
    "make_ap_database": "Print_AP_Database",
    "vdt_file_extension": "vdt_file_extension",
    "make_cross_section_file": "XS_Out_File",
    "make_representative_cross_section_file": "Build_Representative_Cross_Section",
    # Mapping
    "use_parquet": "use_parquet",
    "fldpln_library_args.solver": "solver",
    "fldpln_library_args.dh": "dh",
    "fldpln_library_args.iterative_spill": "iterative_spill",
    "fldpln_library_args.parallel": "parallel",
    "fldpln_library_args.fldmn": "fldmn",
    "fldpln_library_args.fldmx": "fldmx",
    "fldpln_library_args.spill_decay": "spill_decay",
    "fldpln_library_args.max_cells_per_segment": "max_cells_per_segment",
    "fldpln_library_args": "fldpln_library_args",
    "curve2flood_args.FLDPLN_Min_Flood_Depth": "FLDPLN_Min_Flood_Depth",
    "curve2flood_args.Flood_WaterLC_and_STRM_Cells": "Flood_WaterLC_and_STRM_Cells",
    "curve2flood_args.Flood_Bathy_Mask": "Flood_Bathy_Mask",
    "curve2flood_args.TW_MultFact": "TW_MultFact",
    "curve2flood_args.TopWidthPlausibleLimit": "TopWidthPlausibleLimit",
    "curve2flood_args": "curve2flood_args",
    "make_depth_maps": "OutDEP",
    "make_velocity_maps": "OutVEL",
    "make_wse_maps": "OutWSE",
    "curve2flood_args.Make_Output_GPKG": "Make_Output_GPKG",
    "floodmap_identifier": "floodmap_identifier",
    # FIST, Go Consequences
    "make_fist_inputs": "make_fist_inputs",
    "estimate_consequences": "estimate_consequences",
    # Run
    "overwrite": "overwrite",
    "overwrite_floodmaps": "overwrite_floodmaps",
    "raise_errors_if_nothing_in_domain": "raise_errors_if_nothing_in_domain",
    "num_workers": "num_workers",
    "profile": "profile",
    "quiet": "quiet",
    "use_yaml": "use_yaml",
    "folder_paths": "folder_paths",
    "file_names": "file_names",
}
# The same for the options of a choice: (row name, option) -> setting
UNDERLYING_CHOICE_NAMES = {
    ("use_specified_depth_for_bathy_mask", "depth"): "Set_Depth",
    ("use_specified_depth_for_bathy_mask", "water"): "BathyWaterMask",
}


def _streamflow_label(value):
    """The streamflow source's label, from a label or a config value ('GEOGLOWS' is GEOGLOWS V2)."""
    if value in STREAMFLOW_SOURCE_LABELS:
        return value
    try:
        source = str(StreamflowSource.from_string(str(value)))
    except ValueError:
        return next(iter(STREAMFLOW_SOURCE_LABELS))
    return next(label for label, known in STREAMFLOW_SOURCE_LABELS.items() if known == source)


def _floodmap_mode_label(value):
    for label, mode in FLOODMAP_MODE_LABELS.items():
        if value in {label, mode}:
            return label
    return next(iter(FLOODMAP_MODE_LABELS))


# What QtLogStream sends when a line it was rewriting with carriage returns ends
END_OF_REWRITTEN_LINE = "\n"


class QtLogStream(io.TextIOBase):
    """
    A text stream that sends each line to a Qt signal. A line written after a carriage return is sent with one in
    front, to replace the one before, as progress bars do; when such a line ends with a newline, the stream sends
    END_OF_REWRITTEN_LINE.
    """
    def __init__(self, qt_signal):
        super().__init__()
        self.qt_signal = qt_signal
        self._buffer = ""
        self._replace_next = False

    def write(self, text):
        if not text:
            return 0

        for index, char in enumerate(text):
            if char == "\r":
                if self._buffer.strip():
                    self.qt_signal.emit(("\r" if self._replace_next else "") + self._buffer)
                self._buffer = ""
                self._replace_next = True
            elif char == "\n":
                if self._buffer.strip():
                    self.qt_signal.emit(("\r" if self._replace_next else "") + self._buffer)
                # Only a newline that ends what was written ends the line: tqdm also writes newlines to move
                # between the lines of nested bars
                if self._replace_next and not text[index + 1:].strip():
                    self.qt_signal.emit(END_OF_REWRITTEN_LINE)
                self._buffer = ""
                self._replace_next = False
            else:
                self._buffer += char

        if self._replace_next and self._buffer.strip():
            self.qt_signal.emit("\r" + self._buffer)
            self._buffer = ""
        return len(text)

    def flush(self):
        # Sends what is waiting, but doesn't end the line: tqdm flushes after every update of a bar
        if self._buffer.strip():
            self.qt_signal.emit(("\r" if self._replace_next else "") + self._buffer)
        self._buffer = ""


# A tqdm progress bar, as '<desc>: 45%|####5     | 9/20 [00:01<00:02, 4.50it/s]', or without a total,
# '<desc>: 9it [00:01, 4.50it/s]'; and the terminal escapes nested bars move between lines with
_PROGRESS_BAR = re.compile(r"^(?P<desc>.*?)\s*(?:\d{1,3}%\||\d+(?:\.\d+)?[kMGTPEZY]?it \[)")
_PROGRESS_TIME = re.compile(r"\[\d+:\d{2}(?::\d{2})?(?:<[\d:?]+)?(?:,[^\]]*)?\]")
_PROGRESS_COUNT = re.compile(r"\|\s*(?P<n>[\d.]+[kMGTPEZY]?)/(?P<total>[\d.]+[kMGTPEZY]?)\s*\[")
_TERMINAL_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _progress_bar(text):
    """
    ``(desc, finished)`` of a tqdm progress bar line, or None if it isn't one. A bar with a total is finished at
    100%; one without, never (its line ending finishes it).
    """
    match = _PROGRESS_BAR.match(text)
    if not match or not _PROGRESS_TIME.search(text):
        return None
    count = _PROGRESS_COUNT.search(text)
    finished = bool(re.search(r"(?<!\d)100%\|", text)) or bool(count and count["n"] == count["total"])
    return match["desc"].rstrip(": ").strip(), finished


class QtLogHandler(logging.Handler):
    """A logging handler that sends each message to a Qt signal."""
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        try:
            self.signal.emit(self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)


class LogRelay(QObject):
    """
    Carries log messages and printed text to the window from any thread. The window keeps one for as long as it is
    open, since a run's stream outlives the run: ARC and Curve2Flood give their loggers the sys.stdout they are first
    imported under, the first run's stream, and write to it in every run after. When each run's stream sent through
    its own WorkerThread, the next run's replacing that worker left the stream emitting from a deleted object (PyQt
    keeps no reference to a bound signal's object), and ARC's first warning crashed the GUI.
    """
    message = pyqtSignal(str)


class WorkerThread(QThread):
    """
    Runs process_watershed in the background, sending its log and printed text to ``log_signal`` (a LogRelay's
    message, which must outlive the run; by default its own log_signal) and any error to the window.
    """
    finished_signal = pyqtSignal()
    log_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, params: dict, log_signal=None):
        super().__init__()
        self.params = dict(params)
        self.log_target = self.log_signal if log_signal is None else log_signal

    @staticmethod
    def _format_exception(e: Exception, context: str = ""):
        exc_type = type(e).__name__
        exc_msg = str(e)
        location = ""
        code_line = ""
        try:
            frames = traceback.extract_tb(e.__traceback__)
            if frames:
                last = frames[-1]
                location = f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}()"
                code_line = (last.line or "").strip()
        except Exception:  # noqa: BLE001, S110
            pass

        summary_parts = [context] if context else []
        summary_parts.append(f"{exc_type}: {exc_msg}".strip())
        if location:
            summary_parts.append(f"Location: {location}")
        if code_line:
            summary_parts.append(f"Code: {code_line}")
        return "\n".join(summary_parts), traceback.format_exc()

    def run(self):
        qt_handler = QtLogHandler(self.log_target)
        qt_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        LOG.addHandler(qt_handler)
        LOG.setLevel(logging.INFO)

        old_stdout, old_stderr = sys.stdout, sys.stderr
        stream = QtLogStream(self.log_target)

        try:
            sys.stdout = stream
            sys.stderr = stream
            from nencarta.main import process_watershed

            LOG.info("Running nencarta.process_watershed()")
            LOG.info(json.dumps(self.params, indent=2, default=str))
            process_watershed(dict(self.params))
            self.finished_signal.emit()
        except Exception as e:  # noqa: BLE001
            summary, details = self._format_exception(e, context="Error running simulation")
            LOG.error(summary.replace("\n", " | "))
            self.error_signal.emit(summary + "\n\nDETAILS:\n" + details)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            LOG.removeHandler(qt_handler)


class PathPicker(QWidget):
    """A text box with a Browse button, for a file or folder. Quotes pasted around a path are dropped."""
    def __init__(self, mode="file", default_path="", dialog_title="Select Path", file_filter="All Files (*)"):
        super().__init__()
        self.mode = mode
        self.dialog_title = dialog_title
        self.file_filter = file_filter
        self.line_edit = QLineEdit(str(default_path or ""))
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.line_edit)
        layout.addWidget(self.browse_btn)

    def _browse(self):
        current = self.text()
        start_dir = _dialog_start_dir(current)
        if self.mode == "directory":
            value = QFileDialog.getExistingDirectory(
                self,
                self.dialog_title,
                start_dir,
                _dialog_options(QFileDialog.ShowDirsOnly, QFileDialog.DontResolveSymlinks),
            )
        else:
            value, _ = QFileDialog.getOpenFileName(
                self,
                self.dialog_title,
                start_dir,
                self.file_filter,
                options=_dialog_options(QFileDialog.DontResolveSymlinks),
            )
        if value:
            _remember_dialog_dir(value)
            self.line_edit.setText(value)

    def text(self):
        return strip_quotes(self.line_edit.text())


class MultiFilePicker(QWidget):
    """A list of files with Add, Remove and Clear buttons, and a box to paste a path into."""
    changed_signal = pyqtSignal()

    def __init__(self, default_paths=None, dialog_title="Select Files", file_filter="All Files (*)"):
        super().__init__()
        self.dialog_title = dialog_title
        self.file_filter = file_filter
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_widget.setMinimumHeight(96)

        self.paste_edit = QLineEdit()
        self.paste_edit.setPlaceholderText("Paste paths here and press Enter")
        self.paste_edit.returnPressed.connect(self._add_pasted)
        add_btn = QPushButton("Add files...")
        remove_btn = QPushButton("Remove")
        clear_btn = QPushButton("Clear")
        add_btn.clicked.connect(self._browse)
        remove_btn.clicked.connect(self.remove_selected)
        clear_btn.clicked.connect(self.clear)

        buttons = QHBoxLayout()
        buttons.addWidget(add_btn)
        buttons.addWidget(remove_btn)
        buttons.addWidget(clear_btn)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.paste_edit)
        layout.addLayout(buttons)

        self.set_values(default_paths or [])

    def _browse(self):
        current = self.values()[-1] if self.values() else ""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            self.dialog_title,
            _dialog_start_dir(current),
            self.file_filter,
            options=_dialog_options(QFileDialog.DontResolveSymlinks),
        )
        if files:
            _remember_dialog_dir(files[0])
        for file_path in files:
            self.add_value(file_path)

    def _add_pasted(self):
        for value in _split_paths(self.paste_edit.text()):
            self.add_value(value)
        self.paste_edit.clear()

    def add_value(self, value):
        value = strip_quotes(str(value))
        if value and value not in self.values():
            self.list_widget.addItem(value)
            self.changed_signal.emit()

    def remove_selected(self):
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))
        self.changed_signal.emit()

    def clear(self):
        self.list_widget.clear()
        self.changed_signal.emit()

    def set_values(self, values):
        old_values = self.values()
        self.clear()
        if isinstance(values, str):
            values = _split_paths(values)
        for value in values:
            self.add_value(value)
        if self.values() != old_values:
            self.changed_signal.emit()

    def values(self):
        return [self.list_widget.item(row).text() for row in range(self.list_widget.count())]


class ReturnPeriodPicker(QWidget):
    """A checkbox for each return period."""
    def __init__(self, selected=None):
        super().__init__()
        selected = {int(value) for value in (selected or [])}
        self.checkboxes = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for period in VALID_RETURN_PERIODS:
            checkbox = QCheckBox(str(period))
            checkbox.setChecked(period in selected)
            self.checkboxes[period] = checkbox
            layout.addWidget(checkbox)
        layout.addStretch(1)

    def values(self):
        return [period for period, checkbox in self.checkboxes.items() if checkbox.isChecked()]

    def set_values(self, values):
        values = {int(value) for value in (values or [])}
        for period, checkbox in self.checkboxes.items():
            checkbox.setChecked(period in values)


class DictTable(QWidget):
    """An editable table of a dictionary's keys and values."""
    def __init__(self, data=None, defaults=None):
        super().__init__()
        self.defaults = dict(defaults or {})
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        add_btn = QPushButton("Add argument")
        remove_btn = QPushButton("Remove")
        reset_btn = QPushButton("Reset defaults")
        add_btn.clicked.connect(self.add_row)
        remove_btn.clicked.connect(self.remove_selected_rows)
        reset_btn.clicked.connect(self.reset_defaults)

        buttons = QHBoxLayout()
        buttons.addWidget(add_btn)
        buttons.addWidget(remove_btn)
        buttons.addWidget(reset_btn)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

        self.set_dict(data or {})

    def add_row(self, key="", value=""):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(key)))
        self.table.setItem(row, 1, QTableWidgetItem(str(value)))

    def remove_selected_rows(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def set_dict(self, data: dict):
        self.table.setRowCount(0)
        for key, value in data.items():
            self.add_row(key, _format_setting_value(value))

    def reset_defaults(self):
        self.set_dict(self.defaults)

    def to_dict(self) -> dict:
        result = {}
        for row in range(self.table.rowCount()):
            key_item = self.table.item(row, 0)
            value_item = self.table.item(row, 1)
            if key_item and key_item.text().strip():
                result[key_item.text().strip()] = _parse_scalar(value_item.text() if value_item else "")
        return result


class ChoiceRow(QWidget):
    """Radio buttons for one of several choices, given as {key: label}."""
    changed = pyqtSignal()

    def __init__(self, choices: dict, selected=None):
        super().__init__()
        self.group = QButtonGroup(self)
        self.buttons = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for key, label in choices.items():
            button = QRadioButton(label)
            self.group.addButton(button)
            self.buttons[key] = button
            layout.addWidget(button)
            button.toggled.connect(self._toggled)
        self.set_value(selected)

    def _toggled(self, checked):
        if checked:
            self.changed.emit()

    def value(self):
        return next((key for key, button in self.buttons.items() if button.isChecked()), next(iter(self.buttons)))

    def set_value(self, key):
        self.buttons.get(key, next(iter(self.buttons.values()))).setChecked(True)


class DepthsWidget(QWidget):
    """
    The flood depths of specify_depths_for_bathy_mask: the bathymetry's, and with the DEM cleaner on, the cleaner's,
    which the list puts first. Only the cleaner's outlines anything: ARC's channel is burned into the DEM clipped to
    no outline, so the bathymetry's depth changes nothing, but the config still takes one.
    """
    changed = pyqtSignal()

    def __init__(self, depths=None):
        super().__init__()
        depths = [str(value) for value in (depths or [])] or ["0.1"]
        cleaner, bathymetry = (depths[0], depths[1]) if len(depths) > 1 else (depths[0], depths[0])
        self.bathymetry_edit = QLineEdit(bathymetry)
        self.cleaner_edit = QLineEdit(cleaner)
        self.cleaner_label = QLabel("For the DEM cleaner (m)")
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("For the bathymetry (m), not used"), 0, 0)
        layout.addWidget(self.bathymetry_edit, 0, 1)
        layout.addWidget(self.cleaner_label, 1, 0)
        layout.addWidget(self.cleaner_edit, 1, 1)
        self.bathymetry_edit.textChanged.connect(lambda _: self.changed.emit())
        self.cleaner_edit.textChanged.connect(lambda _: self.changed.emit())
        self._cleaner_shown = True

    def show_cleaner(self, shown: bool):
        self._cleaner_shown = shown
        self.cleaner_label.setVisible(shown)
        self.cleaner_edit.setVisible(shown)

    def values(self):
        depths = [self.bathymetry_edit.text()]
        if self._cleaner_shown:
            depths.insert(0, self.cleaner_edit.text())
        return [float(value) for value in depths if value.strip()]

    def state(self):
        return [self.cleaner_edit.text().strip(), self.bathymetry_edit.text().strip()]


class TemplateTable(QWidget):
    """
    A dictionary edited as rows of a name picked from ``choices`` and a text. ``tags`` are buttons that add their
    text to the selected row's. Rows with no text are left out, unless ``keep_empty``.
    """
    changed = pyqtSignal()

    def __init__(self, choices, data=None, tags=(), headers=("Name", "Value"), add_label="Add", keep_empty=False):
        super().__init__()
        self.choices = list(choices)
        self.keep_empty = keep_empty
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(list(headers))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)
        self.table.itemChanged.connect(lambda _: self.changed.emit())

        add_btn = QPushButton(add_label)
        remove_btn = QPushButton("Remove")
        add_btn.clicked.connect(lambda: self.add_row())
        remove_btn.clicked.connect(self.remove_selected_rows)
        buttons = QHBoxLayout()
        buttons.addWidget(add_btn)
        buttons.addWidget(remove_btn)
        if tags:
            buttons.addSpacing(12)
            buttons.addWidget(QLabel("Add a tag:"))
            for tag in tags:
                tag_btn = QPushButton(tag)
                tag_btn.setToolTip(f"Add {tag} to the end of the selected row's text")
                tag_btn.clicked.connect(lambda _, tag=tag: self.add_tag(tag))
                buttons.addWidget(tag_btn)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self.set_dict(data or {})

    def add_row(self, name=None, value=""):
        row = self.table.rowCount()
        self.table.insertRow(row)
        combo = QComboBox()
        combo.addItems(self.choices)
        if name is not None:
            if name not in self.choices:
                combo.addItem(str(name))
            combo.setCurrentText(str(name))
        combo.currentTextChanged.connect(lambda _: self.changed.emit())
        self.table.setCellWidget(row, 0, combo)
        self.table.setItem(row, 1, QTableWidgetItem(str(value)))
        self.table.setCurrentCell(row, 1)
        self.changed.emit()

    def remove_selected_rows(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()} or {self.table.currentRow()}, reverse=True)
        for row in rows:
            if row >= 0:
                self.table.removeRow(row)
        self.changed.emit()

    def add_tag(self, tag):
        row = self.table.currentRow()
        if row < 0:
            if self.table.rowCount() == 0:
                self.add_row()
            row = self.table.rowCount() - 1
        item = self.table.item(row, 1)
        text = item.text().rstrip("/\\")
        item.setText(f"{text}/{tag}" if text else tag)

    def set_dict(self, data: dict):
        self.table.setRowCount(0)
        for name, value in (data or {}).items():
            self.add_row(name, "" if value is None else value)

    def to_dict(self) -> dict:
        result = {}
        for row in range(self.table.rowCount()):
            name = self.table.cellWidget(row, 0).currentText().strip()
            item = self.table.item(row, 1)
            value = strip_quotes(item.text()) if item else ""
            if name and (value or self.keep_empty):
                result[name] = value
        return result


def _parse_scalar(value):
    value = str(value).strip()
    if value == "":
        return ""
    
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None
    
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001, S110
        pass

    try:
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        return value


def _format_setting_value(value):
    if isinstance(value, (dict, list, bool, int, float)):
        return json.dumps(value)
    return "" if value is None else str(value)


def _setting_text(key, default="", multiline=False):
    value = settings.value(key, default)
    if value is None:
        return ""
    
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value
        
        if isinstance(parsed, list):
            separator = "\n" if multiline else ", "
            return separator.join(str(item) for item in parsed)
        return str(parsed)
    
    if isinstance(value, list):
        separator = "\n" if multiline else ", "
        return separator.join(str(item) for item in value)
    
    return str(value)


def _setting_list(key, default=None):
    value = settings.value(key, default if default is not None else [])
    if value is None:
        return []
    
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return _split_list(value)
        if isinstance(parsed, list):
            return parsed
        return [str(parsed)]
    
    if isinstance(value, list):
        return value
    return [value]


def _default(key, fallback=""):
    return DEFAULT_CONFIG.get(key, fallback)


def _help(key):
    """The config key's description, shown as its field's tooltip."""
    option = CONFIG_OPTIONS.get("name" if key == "watershed_name" else key)
    return option.help if option else None


def _split_list(value):
    """Items separated by commas or lines, without quotes around them."""
    text = str(value or "").replace(",", "\n")
    return [strip_quotes(part) for part in text.splitlines() if strip_quotes(part)]


def _split_paths(value):
    """
    Paths one to a line, or several quoted on one line, as Windows' "Copy as path" gives them. Commas stay, since
    paths can have them.
    """
    paths = []
    for line in str(value or "").splitlines():
        quoted = re.findall(r'"([^"]+)"', line)
        paths.extend(quoted or [line])

    return [strip_quotes(path) for path in paths if strip_quotes(path)]


def _parse_float_list(value):
    return [float(part) for part in _split_list(value)]


def _parse_int_list(value):
    return [int(float(part)) for part in _split_list(value)]


def _parse_bbox(value):
    parts = _parse_float_list(value)
    if not parts:
        return None
    
    if len(parts) != 4:
        raise ValueError("bbox must contain four values: minx, miny, maxx, maxy.")
    
    return parts


def _is_network_path(path_text):
    text = str(path_text or "").strip()
    return text.startswith(("\\\\", "//"))


def _local_dialog_dir(path_text):
    text = str(path_text or "").strip()
    if not text or _is_network_path(text):
        return None
    
    try:
        path = Path(text).expanduser()
        if path.is_dir():
            return str(path)
        if path.is_file():
            return str(path.parent)
        if path.parent.is_dir():
            return str(path.parent)
    except (OSError, RuntimeError, ValueError):
        return None
    
    return None


def _dialog_start_dir(path_text=""):
    for candidate in (
        path_text,
        settings.value("last_file_dialog_dir", ""),
        str(Path.home()),
    ):
        directory = _local_dialog_dir(candidate)
        if directory:
            return directory
        
    return ""


def _remember_dialog_dir(path_text):
    if not path_text or _is_network_path(path_text):
        return
    
    try:
        path = Path(path_text).expanduser()
        directory = path if path.is_dir() else path.parent
        if directory.is_dir():
            settings.setValue("last_file_dialog_dir", str(directory))
    except (OSError, RuntimeError, ValueError):
        return


def _dialog_options(*options):
    combined = QFileDialog.DontUseNativeDialog
    for option in options:
        combined |= option

    return combined


class FloodSimulationGUI(QMainWindow):
    """
    The main window: the settings in tabs, a preview of them, and the run's log.

    Each field is a row that a rule can hide when it doesn't apply, e.g. the FLDPLN settings unless the mapper is
    FLDPLN. A hidden field isn't sent, so it takes its default: what the form shows is what runs.
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NenCarta")
        icon_path = Path(__file__).with_name("images") / "header_logo.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(1320, 860)
        self.input_fields = {}   # config key (and watershed_name for name) -> widget
        self.field_parsers = {}  # config key -> how its widget's value is read
        self.arg_fields = {}     # (arc_args, curve2flood_args or fldpln_library_args, setting) -> (widget, parser)
        self.choices = {}        # the form's own choices (DEM source, ...) -> widget
        self.rows = {}           # row name -> its widgets, to show or hide
        self.rules = []          # (row names, whether to show them)
        self.hidden = set()
        self._saved_args = {key: {arg: value for arg, value in _saved_dict(key, ARG_DEFAULTS[key]).items()
                                  if arg not in RETIRED_ARGS} for key in ARG_FIELDS}
        self.worker_thread = None
        # The runs' log comes through this, which lives as long as the window (see LogRelay)
        self.log_relay = LogRelay(self)
        self.log_relay.message.connect(self.log_message)
        self._bars = {}  # each progress bar -> the log's line (block) it is on
        self._finished_bars = set()  # bars whose lines only their finished state may rewrite, as tqdm's close does
        self._last_bar = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._refresh_previews)
        self._init_ui()

    def _init_ui(self):
        self.central_widget = QWidget()
        self.central_widget.setObjectName("centralWidget")
        self.setCentralWidget(self.central_widget)

        root_layout = QVBoxLayout(self.central_widget)
        root_layout.setContentsMargins(18, 16, 18, 16)
        root_layout.setSpacing(14)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)

        title_block = QVBoxLayout()
        title = QLabel("NenCarta")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Runner and monitor")
        subtitle.setObjectName("AppSubtitle")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)

        self.validate_button = QPushButton("Validate")
        self.validate_button.clicked.connect(self.validate_configuration)
        self.preview_button = QPushButton("Preview JSON")
        self.preview_button.clicked.connect(self.preview_configuration)
        self.run_button = QPushButton("Start Simulation")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.clicked.connect(self.start_simulation)
        self.run_button.setMinimumHeight(40)

        header_layout.addLayout(title_block)
        header_layout.addStretch(1)
        header_layout.addWidget(self.validate_button)
        header_layout.addWidget(self.preview_button)
        header_layout.addWidget(self.run_button)
        root_layout.addWidget(header)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMinimumWidth(600)
        splitter.addWidget(self.tabs)

        monitor_panel = QWidget()
        monitor_layout = QVBoxLayout(monitor_panel)
        monitor_layout.setContentsMargins(12, 0, 0, 0)
        monitor_layout.setSpacing(12)

        preview_group = QGroupBox("Configuration Preview")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setFont(QFont("Consolas", 9))
        self.preview_text.setMaximumHeight(230)
        preview_layout.addWidget(self.preview_text)
        monitor_layout.addWidget(preview_group)

        log_group = QGroupBox("Simulation Log")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_text)
        monitor_layout.addWidget(log_group, 1)

        splitter.addWidget(monitor_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        self._add_input_tabs()
        self._show_underlying_names()
        self._connect_changes()
        self._refresh_visibility()
        self._refresh_previews()
        self.log_text.setText("[INFO] Configure inputs, then start the simulation.\n")

    # ------------------------------------------------------------------------------------------------------------
    # Building the form
    # ------------------------------------------------------------------------------------------------------------

    def _make_tab(self, title):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        layout.addStretch(1)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, title)
        return layout

    def _make_group(self, parent_layout, title):
        group = QGroupBox(title)
        layout = QGridLayout(group)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(10)
        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 1)
        parent_layout.insertWidget(parent_layout.count() - 1, group)
        return layout

    def _place(self, layout, name, label, widget, tooltip=None):
        """Put a labelled widget (or, with no label, a full-width one) on a new row, called ``name``."""
        row = layout.rowCount()
        if tooltip:
            widget.setToolTip(tooltip)
        if label is None:
            layout.addWidget(widget, row, 0, 1, 2)
            self.rows[name] = [widget]
        else:
            label_widget = QLabel(label)
            label_widget.setWordWrap(True)
            if tooltip:
                label_widget.setToolTip(tooltip)
            layout.addWidget(label_widget, row, 0)
            layout.addWidget(widget, row, 1)
            self.rows[name] = [label_widget, widget]
        return widget

    def _add_row(self, layout, label, widget, key, parser="text", tooltip=None):
        self.input_fields[key] = widget
        self.field_parsers[key] = parser
        tooltip = tooltip or _help(key)
        config_key = "name" if key == "watershed_name" else key
        return self._place(layout, key, label, widget, f"{config_key}: {tooltip}" if tooltip else config_key)

    def _show_underlying_names(self):
        """Put the setting behind each field whose label doesn't name it after its label (see UNDERLYING_NAMES)."""
        for name, underlying in UNDERLYING_NAMES.items():
            label = self.rows[name][0]
            label.setText(f"{label.text()} ({underlying})")
        for (name, option), underlying in UNDERLYING_CHOICE_NAMES.items():
            button = self.choices[name].buttons[option]
            button.setText(f"{button.text()} ({underlying})")

    def _add_checkbox(self, layout, key, label, default=False, tooltip=None, inverted=False):
        """A checkbox for a true/false key; ``inverted`` ticks it when the key is false, as 'Carve bathymetry'."""
        widget = QCheckBox(label)
        checked = settings.value(key, default, type=bool)
        widget.setChecked(not checked if inverted else checked)
        return self._add_row(layout, None, widget, key, "inverted_bool" if inverted else "bool", tooltip)

    def _add_line(self, layout, key, label, default="", parser="text", placeholder="", tooltip=None):
        widget = QLineEdit(_setting_text(key, default))
        widget.setPlaceholderText(placeholder)
        return self._add_row(layout, label, widget, key, parser, tooltip)

    def _add_combo(self, layout, key, label, items, default=None, editable=False, parser="text", tooltip=None,
                   saved=None):
        saved = settings.value(key, default if default is not None else "") if saved is None else saved
        widget = QComboBox()
        widget.setEditable(editable)
        widget.addItems([str(item) for item in items])
        if saved is not None:
            widget.setCurrentText(str(saved))
        return self._add_row(layout, label, widget, key, parser, tooltip)

    def _add_spin(self, layout, key, label, default=0, minimum=0, maximum=999999, tooltip=None):
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(settings.value(key, default, type=int))
        return self._add_row(layout, label, widget, key, "int", tooltip)

    def _add_path(self, layout, key, label, default="", mode="file", file_filter="All Files (*)", tooltip=None):
        widget = PathPicker(mode=mode, default_path=_setting_text(key, default), dialog_title=f"Select {label}",
                            file_filter=file_filter)
        return self._add_row(layout, label, widget, key, "path", tooltip)

    def _add_multi_files(self, layout, key, label, default=None, file_filter="All Files (*)", tooltip=None):
        widget = MultiFilePicker(_setting_list(key, default), dialog_title=f"Select {label}", file_filter=file_filter)
        return self._add_row(layout, label, widget, key, "str_list", tooltip)

    def _add_return_periods(self, layout, key, label, default=None, tooltip=None):
        widget = ReturnPeriodPicker(_setting_list(key, default))
        return self._add_row(layout, label, widget, key, "int_list", tooltip)

    def _add_dict(self, layout, key, label, data, defaults=None, tooltip=None, height=200):
        widget = DictTable(data, defaults=data if defaults is None else defaults)
        widget.setMinimumHeight(height)
        if height < 200:
            widget.table.setMaximumHeight(height - 40)
        return self._add_row(layout, label, widget, key, "dict", tooltip)

    def _add_choice(self, layout, name, label, choices, selected, config_key=False, tooltip=None):
        """Radio buttons for one of ``choices``: a config key's value, or a choice of the form's own."""
        widget = ChoiceRow(choices, selected)
        self.choices[name] = widget
        if config_key:
            return self._add_row(layout, label, widget, name, "choice", tooltip)
        return self._place(layout, name, label, widget, tooltip)

    def _add_toggle(self, layout, name, label, checked, tooltip=None):
        """A checkbox of the form's own, which shows or hides other fields."""
        widget = QCheckBox(label)
        widget.setChecked(bool(checked))
        self.choices[name] = widget
        return self._place(layout, name, None, widget, tooltip)

    def _add_note(self, layout, name, text, warning=False):
        widget = QLabel(text)
        widget.setWordWrap(True)
        widget.setObjectName("WarningNote" if warning else "Note")
        return self._place(layout, name, None, widget)

    def _add_arg(self, layout, dict_key, arg, label, parser="float", items=None, tooltip=None, placeholder=None):
        """A field for one setting of ``dict_key`` (arc_args, curve2flood_args or fldpln_library_args): the one the
        form remembered, else nencarta's starting value, else the model's default."""
        value = self._saved_args[dict_key].get(arg, ARG_DEFAULTS[dict_key].get(arg))
        if value is None:
            value = _model_default(dict_key, arg)
        if value is None:
            value = ""
        if parser == "bool":
            widget = QCheckBox(label)
            widget.setChecked(_parse_scalar(value) is True)
            label = None
        elif items is not None:
            widget = QComboBox()
            widget.addItems(list(items))
            widget.setCurrentText(str(value))
        else:
            widget = QLineEdit(_format_setting_value(value))
            if placeholder:
                widget.setPlaceholderText(placeholder)
        self.arg_fields[(dict_key, arg)] = (widget, parser)
        return self._place(layout, f"{dict_key}.{arg}", label, widget, tooltip)

    def _add_arg_table(self, layout, dict_key, label, tooltip):
        """The table of ``dict_key``'s other arguments: those without fields of their own."""
        fields = ARG_FIELDS[dict_key]
        extras = {key: value for key, value in self._saved_args[dict_key].items() if key not in fields}
        defaults = {key: value for key, value in ARG_DEFAULTS[dict_key].items() if key not in fields}
        return self._add_dict(layout, dict_key, label, extras, defaults, tooltip)

    def show_when(self, predicate, *names):
        """Show the rows ``names`` only while ``predicate()`` is true."""
        self.rules.append((names, predicate))

    def _indent(self, *names):
        """Indent rows under the option they belong to."""
        for name in names:
            first = self.rows[name][0]
            first.setStyleSheet("margin-left: 22px;")

    def _add_input_tabs(self):
        self._add_inputs_tab()
        self._add_preprocessing_tab()
        self._add_arc_tab()
        self._add_mapping_tab()
        self._add_fist_tab()
        self._add_consequences_tab()
        self._add_run_tab()

    def _add_inputs_tab(self):
        tab = self._make_tab("Inputs")
        raster_files = "Raster Files (*.tif *.tiff *.vrt *.img);;All Files (*)"
        source_files = ("Raster/Vector Files (*.tif *.tiff *.vrt *.img *.gpkg *.shp *.gdb *.parquet *.geoparquet);;"
                        "All Files (*)")
        vector_files = "Vector Files (*.gpkg *.shp *.gdb *.parquet *.geoparquet *.geojson *.json);;All Files (*)"

        group = self._make_group(tab, "Configuration")
        self._add_line(group, "watershed_name", "Watershed name", settings.value("name", "example"), placeholder="example")
        self._add_path(group, "output_dir", "Output folder", "", mode="directory")

        group = self._make_group(tab, "DEM")
        self._add_choice(group, "dem_mode", "DEM source", DEM_MODES, _saved_dem_mode())
        self._add_path(group, "dem", "DEM file", "", file_filter=raster_files)
        self._add_path(group, "dem_dir", "DEM folder", "", mode="directory")
        self._add_line(group, "dem_filter", "DEM file pattern", _default("dem_filter"), placeholder="*.tif")
        self._add_line(group, "bbox", "Bounding box (degrees)", "", parser="bbox", placeholder="minx, miny, maxx, maxy")
        self._add_checkbox(group, "buffer", "Buffer the DEM: cut a larger one from source DEMs", _default("buffer"))
        self._add_line(group, "buffer_distance", "Buffer distance (degrees)", _default("buffer_distance"), parser="float")
        self._add_multi_files(group, "source_dems", "Source DEMs", _default("source_dems"), source_files)
        self._add_checkbox(group, "project_to_utm", "Project the cut DEM to its UTM zone", _default("project_to_utm"))
        self._add_checkbox(group, "use_vrt", "Write the cut DEM as a VRT", _default("use_vrt"))
        self._add_checkbox(group, "use_warning_flags_to_download_dem",
                           "Download USGS DEMs into the folder where GEOGLOWS forecasts flooding (CONUS)",
                           _default("use_warning_flags_to_download_dem"))
        self._add_combo(group, "geoglows_vpu", "GEOGLOWS V2 VPU", ["", "704", "702", "703", "715", "714", "706", "713",
                                                                "712", "709"], default="", editable=True)
        dem_mode = lambda: self.choices["dem_mode"].value()
        cut = lambda: dem_mode() == "bbox" or self._on("buffer")
        self.show_when(lambda: dem_mode() == "file", "dem")
        self.show_when(lambda: dem_mode() == "folder", "dem_dir", "dem_filter", "use_warning_flags_to_download_dem")
        self.show_when(lambda: dem_mode() == "bbox", "bbox")
        self.show_when(cut, "source_dems", "project_to_utm", "use_vrt")
        self.show_when(lambda: self._on("buffer"), "buffer_distance")
        self.show_when(lambda: dem_mode() == "folder" and self._on("use_warning_flags_to_download_dem"), "geoglows_vpu")
        self._indent("buffer_distance", "geoglows_vpu")

        group = self._make_group(tab, "Hydrography")
        self._add_choice(group, "flowline_mode", "Stream network", FLOWLINE_MODES, _saved_flowline_mode())
        self._add_path(group, "flowline", "Stream network file", "", file_filter=vector_files)
        self._add_multi_files(group, "source_flowlines", "Stream network files", _default("source_flowlines"), vector_files)
        self._add_combo(group, "streamflow_source", "Streamflow source", STREAMFLOW_SOURCE_LABELS.keys(),
                        saved=_streamflow_label(settings.value("streamflow_source", _default("streamflow_source"))))
        self._add_line(group, "nwm_api_key", "NWM API key", "", placeholder="Or set the NWM_API_KEY environment variable")
        self._add_line(group, "stream_id_field", "Reach ID field", "")
        self._add_line(group, "downstream_id_field", "Downstream reach ID field", "")
        self._add_checkbox(group, "streams_as_parquet", "Write the stream networks as GeoParquet",
                           _default("streams_as_parquet"))
        self._add_line(group, "exclude", "Leave out reaches", "", parser="int_list", placeholder="Reach IDs: 123, 456")
        flowline_mode = lambda: self.choices["flowline_mode"].value()
        self.show_when(lambda: flowline_mode() == "file", "flowline")
        self.show_when(lambda: flowline_mode() == "many", "source_flowlines")
        self.show_when(self._is_nwm, "nwm_api_key")

    def _add_preprocessing_tab(self):
        tab = self._make_tab("Preprocessing")
        csv_files = "CSV Files (*.csv);;All Files (*)"
        flow_datasets = "Streamflow Datasets (*.csv *.parquet *.nc *.zarr);;All Files (*)"
        vector_files = "Vector Files (*.gpkg *.shp *.gdb *.parquet *.geoparquet *.geojson *.json);;All Files (*)"

        group = self._make_group(tab, "Flow")
        self._add_note(group, "flow_note", "")
        self._add_combo(group, "specified_bathyflow_field", "Flow for the bathymetry (m^3/s)", FLOW_FIELD_OPTIONS,
                        default=_default("specified_bathyflow_field"), editable=True)
        self._add_combo(group, "specified_highflow_field", "Highest flow for the rating curves (m^3/s)", FLOW_FIELD_OPTIONS,
                        default=_default("specified_highflow_field"), editable=True)
        self._add_path(group, "reanalysis_file", "Flow statistics file", "", file_filter=csv_files)
        self._add_path(group, "return_period_file", "Return period flows file", "", file_filter=flow_datasets)
        self._add_line(group, "return_period_variables", "Return period variables", "", parser="str_list",
                       placeholder="Optional: all of them")
        self._add_checkbox(group, "include_fdc", "Add flow duration curve flows (p_exceed_...)", _default("include_fdc"))
        self._add_path(group, "fdc_file", "Flow duration curve file", "", file_filter=flow_datasets)
        self._add_line(group, "fdc_variable", "Flow duration curve variable", "",
                       placeholder="Optional: hourly_annual, or the only one")
        self._add_checkbox(group, "raise_errors_if_river_ids_missing", "Stop if reaches are missing from the flow data",
                           _default("raise_errors_if_river_ids_missing"))
        self._add_dict(group, "reanalysis_storage_options", "Remote dataset access (optional)", _saved_dict(
            "reanalysis_storage_options", {}), {}, height=120)
        self._add_combo(group, "floodmap_mode", "Flood maps of", FLOODMAP_MODE_LABELS.keys(),
                        saved=_floodmap_mode_label(settings.value("floodmap_mode", _default("floodmap_mode"))))
        self._add_multi_files(group, "user_flow_files", "User flow files", _default("user_flow_files"), csv_files)
        self._add_return_periods(group, "return_periods", "Return periods", _default("return_periods"))
        self._add_line(group, "forensic_forecast_date", "Forecast date", "", placeholder="YYYYMMDD; default: the newest")
        self._add_combo(group, "forensic_forecast_hour", "Forecast hour (UTC)",
                        [""] + [f"{hour:02d}" for hour in range(24)], default="")
        self._add_checkbox(group, "remove_old_forecast_files", "Remove old forecast flood maps",
                           _default("remove_old_forecast_files"))
        self._add_spin(group, "age_of_forecast_days", "Remove them after (days)", _default("age_of_forecast_days"), 1, 365)
        mode = self._floodmap_mode
        self.show_when(self._is_custom, "return_period_file", "return_period_variables")
        self.show_when(lambda: not self._is_nwm(), "include_fdc", "raise_errors_if_river_ids_missing",
                       "reanalysis_storage_options")
        self.show_when(lambda: not self._is_nwm() and self._on("include_fdc"), "fdc_file", "fdc_variable")
        self.show_when(lambda: mode() == FloodMapMode.USER.value, "user_flow_files")
        self.show_when(lambda: mode() == FloodMapMode.RETURN_PERIOD.value, "return_periods")
        self.show_when(lambda: mode() == FloodMapMode.FORECAST.value or self._downloads_dems(), "forensic_forecast_date")
        self.show_when(lambda: mode() == FloodMapMode.FORECAST.value and self._is_nwm(), "forensic_forecast_hour")
        self.show_when(lambda: mode() == FloodMapMode.FORECAST.value, "remove_old_forecast_files")
        self.show_when(lambda: mode() == FloodMapMode.FORECAST.value and self._on("remove_old_forecast_files"),
                       "age_of_forecast_days")
        self._indent("fdc_file", "fdc_variable", "age_of_forecast_days")

        group = self._make_group(tab, "Hydrography")
        self._add_checkbox(group, "clean_dem", "Clean the DEM: smooth its stream channels first", _default("clean_dem"))
        self._add_checkbox(group, "move_stream_network_to_thalweg",
                           "Move the streams onto the DEM's flow paths (FLDPLN needs this)",
                           _default("move_stream_network_to_thalweg"))
        self._add_checkbox(group, "relocate_misplaced_streams",
                           "Before moving them, redraw the reaches the DEM shows are off their river",
                           _default("relocate_misplaced_streams"))
        self._add_checkbox(group, "minimize_output_files", "After moving them, delete the source and redrawn streams",
                           _default("minimize_output_files"))
        self._add_line(group, "new_strm_threshold_km2", "Smallest drainage area of the flow paths to move onto (km²)",
                       _default("new_strm_threshold_km2"), parser="float")
        self._add_checkbox(group, "burn_streams", "Burn the streams into the DEM, ensuring each channel is properly represented", _default("burn_streams"))
        self._add_checkbox(group, "use_dem_derived_channel_mask",
                           "Find the channel the DEM shows: smooth the water it flattens, don't burn lines on it, "
                           "and add the channel to the water mask", _default("use_dem_derived_channel_mask"))
        self._add_combo(group, "dem_flattens_rivers", "The DEM flattens every river wide enough to show",
                        CONFIG_OPTIONS["dem_flattens_rivers"].choices, default=_default("dem_flattens_rivers"),
                        tooltip="yes for FABDEM and Copernicus: the low ground by a river they left unflattened is "
                                "floodplain, not channel. no for USGS 3DEP, which flattens only some of its water. "
                                "auto: yes when the flat water by the streams is mostly at whole half metres.")
        self._add_checkbox(group, "drop_multilinestrings", "Leave out reaches drawn in pieces (multilinestrings)",
                           _default("drop_multilinestrings"))
        self._add_line(group, "q_baseflow_threshold", "Leave out reaches whose baseflow is below (m³/s)", "",
                       parser="float", placeholder="Optional")
        self._add_line(group, "StrmOrder_Field", "Stream order field", "", placeholder="Optional, e.g. strmOrder")
        self._add_line(group, "area_m2_field", "Drainage area field (m²)", _default("area_m2_field"))
        self._add_line(group, "area_km2_field", "Drainage area field (km²)", _default("area_km2_field"))
        self._add_note(group, "area_note", "The drainage area is needed by the bathymetry's power laws and by "
                                           "redrawing misplaced streams. The m² field is used where the km² one is "
                                           "missing; otherwise both are optional.")
        self._add_toggle(group, "filter_by_order", "Process only some stream orders",
                         _saved_toggle("filter_by_order", bool(_setting_text("StrmOrder_Lower") or
                                                               _setting_text("StrmOrder_Upper"))))
        self._add_line(group, "StrmOrder_Lower", "Lowest stream order", "", parser="int")
        self._add_line(group, "StrmOrder_Upper", "Highest stream order", "", parser="int")
        moving = lambda: self._on("move_stream_network_to_thalweg")
        self.show_when(moving, "relocate_misplaced_streams", "minimize_output_files", "new_strm_threshold_km2")
        self.show_when(lambda: self._on("burn_streams"), "use_dem_derived_channel_mask")
        self.show_when(lambda: self._on("burn_streams") and self._on("use_dem_derived_channel_mask"),
                       "dem_flattens_rivers")
        self.show_when(lambda: self.choices["filter_by_order"].isChecked(), "StrmOrder_Lower", "StrmOrder_Upper")
        self._indent("relocate_misplaced_streams", "minimize_output_files", "new_strm_threshold_km2",
                     "use_dem_derived_channel_mask", "dem_flattens_rivers", "StrmOrder_Lower", "StrmOrder_Upper")

        group = self._make_group(tab, "Lakes and Ocean")
        self._add_note(group, "lakes_note", "Use one of these for lakes. With the Curve2Flood-FLDPLNpy mapper, use the lake polygons.")
        self._add_path(group, "lake_filter_json", "Lake filter JSON", "", file_filter="JSON Files (*.json);;All Files (*)")
        self._add_path(group, "lakes", "Lake polygons", "", file_filter=vector_files)
        self._add_path(group, "ocean", "Ocean polygons, which the burned DEM has no data in", "",
                       file_filter=vector_files)
        self.show_when(lambda: self._on("burn_streams"), "ocean")

        group = self._make_group(tab, "Land Cover")
        self._add_toggle(group, "use_land_cover_cache", "Use land cover tiles instead of downloading them",
                         _saved_toggle("use_land_cover_cache", bool(_setting_list("land_cover_cache"))))
        self._add_multi_files(group, "land_cover_cache", "Land cover tiles", _default("land_cover_cache"),
                              "Raster Files (*.tif *.tiff *.vrt *.img);;All Files (*)")
        self._add_line(group, "land_watervalue", "Land cover value of water", _default("land_watervalue"), parser="int")
        self.show_when(lambda: self.choices["use_land_cover_cache"].isChecked(), "land_cover_cache")
        self._indent("land_cover_cache")

        group = self._make_group(tab, "Water Mask")
        self._add_choice(group, "use_specified_depth_for_bathy_mask",
                         "ARC's water mask, whose edge gives the banks and the DEM cleaner's outline",
                         WATER_MASK_MODES, _saved_water_mask_mode(), config_key=True)
        depths = DepthsWidget(_setting_list("specify_depths_for_bathy_mask", _default("specify_depths_for_bathy_mask")))
        self._add_row(group, "Flood depths", depths, "specify_depths_for_bathy_mask", "depths")
        self._add_line(group, "depth_mask_distance", "Distance the stream cells' water surfaces are averaged over (m)",
                       _default("depth_mask_distance"), parser="float")
        self.show_when(lambda: self.choices["use_specified_depth_for_bathy_mask"].value() == "depth",
                       "specify_depths_for_bathy_mask", "depth_mask_distance")

    def _add_arc_tab(self):
        tab = self._make_tab("ARC")

        group = self._make_group(tab, "Cross Sections")
        self._add_arg(group, "arc_args", "Gen_Dir_Dist", "Cells either side to fit the stream's direction to",
                      "int", tooltip="How many cells each way around a stream cell to fit the stream's direction to "
                                     "(Gen_Dir_Dist).")
        self._add_arg(group, "arc_args", "Degree_Manip", "Angle range to search for the narrowest section (°)",
                      "float", tooltip="The range of angles to turn the cross section through to find where the "
                                       "valley is narrowest, half each way. 0, or under twice the step, turns the "
                                       "search off (Degree_Manip).")
        self._add_arg(group, "arc_args", "Degree_Interval", "Angle step (°)", "float",
                      tooltip="The step between those angles (Degree_Interval).")
        self._add_arg(group, "arc_args", "Low_Spot_Range", "Points either side to look for lower ground", "int",
                      tooltip="How many points either side of the stream cell to look along the cross section for "
                              "lower ground; the cross section moves to the lowest. 0 turns this off "
                              "(Low_Spot_Range).")
        self._add_note(group, "low_spot_warning", "With the Curve2Flood-FLDPLNpy mapper, keep this at 0: moving the "
                                                  "cross sections off the stream cells takes the rating curves off "
                                                  "the streams FLDPLN floods from.", warning=True)
        self.show_when(lambda: self._is_fldpln() and self._arg_number("arc_args", "Low_Spot_Range") > 0,
                       "low_spot_warning")

        group = self._make_group(tab, "Bathymetry")
        self._add_checkbox(group, "disable_bathymetry", "Carve bathymetry into the DEM", _default("disable_bathymetry"),
                           inverted=True, tooltip="Estimate the channel below the DEM's water surface (or banks) and "
                                                  "carve it in. Off: map floods on the DEM as it is.")
        self._add_arg(group, "arc_args", "Bathy_Trap_H", "Share of the top width each sloping side takes (0-0.5)",
                      "float", tooltip="0 is a rectangle, 0.5 a triangle (Bathy_Trap_H).")
        self._add_arg(group, "arc_args", "FindBanksBasedOnLandCover", "Find the banks where the land cover's water ends",
                      "bool")
        self._add_arg(group, "arc_args", "Bathy_Use_Banks",
                      "Rebuild the channel below its banks (bankfull), instead of below the DEM's water surface",
                      "bool")
        self._add_arg(group, "arc_args", "coefficient_width", "Width coefficient (power law of the area in km²)",
                      "float", tooltip="For the channels whose banks the water doesn't show (coefficient_width).")
        self._add_arg(group, "arc_args", "exponent_width", "Width exponent", "float")
        self._add_toggle(group, "depth_power_law",
                         "Size the channel's depth from its drainage area (power law) instead of from its flow",
                         _saved_toggle("depth_power_law", False))
        self._add_arg(group, "arc_args", "coefficient_depth", "Depth coefficient", "float", placeholder="e.g. 0.27")
        self._add_arg(group, "arc_args", "exponent_depth", "Depth exponent", "float", placeholder="e.g. 0.21")
        bathymetry = lambda: self._on("disable_bathymetry")  # ticked when it carves bathymetry
        self.show_when(bathymetry, "arc_args.Bathy_Trap_H", "arc_args.FindBanksBasedOnLandCover",
                       "arc_args.Bathy_Use_Banks", "arc_args.coefficient_width", "arc_args.exponent_width",
                       "depth_power_law")
        self.show_when(lambda: bathymetry() and self.choices["depth_power_law"].isChecked(),
                       "arc_args.coefficient_depth", "arc_args.exponent_depth")
        self._indent("arc_args.Bathy_Trap_H", "arc_args.FindBanksBasedOnLandCover", "arc_args.Bathy_Use_Banks",
                     "arc_args.coefficient_width", "arc_args.exponent_width", "depth_power_law",
                     "arc_args.coefficient_depth", "arc_args.exponent_depth")

        group = self._make_group(tab, "Slope")
        self._add_arg(group, "arc_args", "Stream_Slope_Method", "Slope method", "text", items=SLOPE_METHODS,
                      tooltip="How each stream cell's slope is measured (Stream_Slope_Method; see ARC's docs).")
        self._add_arg(group, "arc_args", "Gen_Slope_Dist", "Cells either side the slope is measured over", "int",
                      tooltip="Gen_Slope_Dist. local_fit fits a line to the 2 x this + 1 cells nearest each "
                              "stream cell; end_points doesn't use it.")
        self._add_arg(group, "arc_args", "Slope_Low_Percentile", "Lowest slope percentile", "int")
        self._add_arg(group, "arc_args", "Slope_High_Percentile", "Highest slope percentile", "int")
        slope_method = lambda: self.arg_fields[("arc_args", "Stream_Slope_Method")][0].currentText()
        self.show_when(lambda: slope_method() != "end_points", "arc_args.Gen_Slope_Dist")
        self.show_when(lambda: slope_method() in ("local_average_corrected", "reach_average"),
                       "arc_args.Slope_Low_Percentile", "arc_args.Slope_High_Percentile")

        group = self._make_group(tab, "Manning's n")
        self._add_choice(group, "mannings_mode", "Manning's n of each land cover class", MANNINGS_MODES,
                         _saved_choice("mannings_mode", MANNINGS_MODES,
                                       "file" if _setting_text("mannings_text_file") else "default"))
        self._add_path(group, "mannings_text_file", "Manning's n table", "",
                       file_filter="Text Files (*.txt);;All Files (*)")
        self.show_when(lambda: self.choices["mannings_mode"].value() == "file", "mannings_text_file")

        group = self._make_group(tab, "Advanced")
        self._add_arg(group, "arc_args", "Standard_Step", "Link the water surfaces along the stream network by the "
                                                          "standard step", "bool")
        self._add_arg(group, "arc_args", "Valley_Sections", "Use a seperate cross section orientation for overbank "
                                                            "flow", "bool")
        self._add_arg(group, "arc_args", "Valley_Dir_Widths", "Channel widths along the stream either way for the "
                                                              "valley's direction", "float")
        self._add_arg(group, "arc_args", "Valley_Dir_Dist", "Cells either way, where a reach has no banks", "int")
        self.show_when(lambda: self._arg_on("arc_args", "Valley_Sections"), "arc_args.Valley_Dir_Widths",
                       "arc_args.Valley_Dir_Dist")
        self._indent("arc_args.Valley_Dir_Widths", "arc_args.Valley_Dir_Dist")
        self._add_arg(group, "arc_args", "Drop_Edge_Sections", "Drop rating curves whose water reaches the DEM's edge",
                      "bool", tooltip="The edge cuts their cross sections off, so tiles whose buffers overlap give "
                                      "the same cell different water, a seam where their maps meet.")
        self._add_arg(group, "arc_args", "Edge_Margin", "Distance from the edge that counts as reaching it (m)",
                      "float")
        self.show_when(lambda: self._arg_on("arc_args", "Drop_Edge_Sections"), "arc_args.Edge_Margin")
        self._indent("arc_args.Edge_Margin")
        self._add_combo(group, "outlet_exchange", "Start rivers leaving the DEM from the neighbouring DEMs' water",
                        CONFIG_OPTIONS["outlet_exchange"].choices, default=_default("outlet_exchange"),
                        tooltip="off maps as usual. pass1 rates only the reaches the neighbours need and writes their "
                                "water; pass2, once every DEM has done pass1, maps each DEM from its neighbours' "
                                "water. Needs buffer and the standard step.")
        self._add_path(group, "outlet_exchange_dir", "Folder the DEMs' passes share", "", mode="directory")
        self.show_when(lambda: self.input_fields["outlet_exchange"].currentText() != "off", "outlet_exchange_dir")
        self._indent("outlet_exchange_dir")
        self._add_arg_table(group, "arc_args", "More ARC settings",
                            "Other ARC settings, by their keys in ARC's input file (see ARC's docs), such as "
                            "shallow_factor, deep_factor, k_decay or slope_adjustment_factor.")

        group = self._make_group(tab, "Outputs")
        self._add_checkbox(group, "make_curvefile", "Make the curve file", _default("make_curvefile"))
        self._add_arg(group, "arc_args", "Reach_Average_Curve_File", "One rating curve per reach, not per stream cell",
                      "bool")
        self._add_checkbox(group, "make_vdt", "Make the VDT database", _default("make_vdt"))
        self._add_arg(group, "arc_args", "VDT_Database_NumIterations", "Flows in each rating curve", "int",
                      tooltip="VDT_Database_NumIterations.")
        self._add_checkbox(group, "make_ap_database", "Make the AP database", _default("make_ap_database"))
        self._add_combo(group, "vdt_file_extension", "VDT and AP database file type", ["txt", "csv", "parquet"],
                        default=_default("vdt_file_extension"))
        self._add_checkbox(group, "make_cross_section_file", "Make the cross-section file",
                           _default("make_cross_section_file"))
        self._add_checkbox(group, "make_representative_cross_section_file", "Make representative cross sections",
                           _default("make_representative_cross_section_file"))
        self.show_when(lambda: self._on("make_curvefile"), "arc_args.Reach_Average_Curve_File")
        self.show_when(lambda: self._on("make_vdt"), "arc_args.VDT_Database_NumIterations")
        self.show_when(lambda: self._on("make_vdt") or self._on("make_ap_database"), "vdt_file_extension")
        self._indent("arc_args.Reach_Average_Curve_File", "arc_args.VDT_Database_NumIterations")

    def _add_mapping_tab(self):
        tab = self._make_tab("Mapping")

        group = self._make_group(tab, "Mapper")
        self._add_combo(group, "mapper", "Mapper", Mapper.list_names(), default=_default("mapper"))
        self._add_checkbox(group, "use_parquet", "Write the FLDPLN stream info table as Parquet", _default("use_parquet"))
        self._add_arg(group, "fldpln_library_args", "solver", "FLDPLN solver", "text", items=("iterative", "exact"),
                      tooltip="Curve2Flood's build_fldpln_library: iterative steps up by the depth step and spills "
                              "once per step; exact spills as far as the water can run downhill.")
        self._add_arg(group, "fldpln_library_args", "dh", "Depth step (m)", "float")
        self._add_arg(group, "fldpln_library_args", "iterative_spill", "Keep routing water that spills over low divides",
                      "bool")
        self._add_arg(group, "fldpln_library_args", "parallel", "Build the FLDPLN library with several processes",
                      "bool")
        self._add_arg(group, "fldpln_library_args", "fldmn", "Least depth a spill stops at (m)", "float")
        self._add_arg(group, "curve2flood_args", "FLDPLN_Min_Flood_Depth",
                      "Shallowest water that floods a cell in the flood map (m)", "float")
        self._add_arg(group, "fldpln_library_args", "fldmx", "Deepest flood the library covers (m)", "float")
        self._add_arg(group, "fldpln_library_args", "spill_decay", "Head a spill loses per cell (m)", "float")
        self._add_arg(group, "fldpln_library_args", "max_cells_per_segment", "Most cells one segment may flood", "int",
                      placeholder="Optional")
        self._add_arg_table(group, "fldpln_library_args", "More FLDPLN library arguments",
                            "Other arguments of Curve2Flood's build_fldpln_library (see Curve2Flood's docs).")
        self._add_arg(group, "curve2flood_args", "Flood_WaterLC_and_STRM_Cells",
                      "Flood the land cover's water and the stream cells", "bool")
        self._add_arg(group, "curve2flood_args", "Flood_Bathy_Mask", "Flood the bathymetry water mask", "bool")
        self._add_arg(group, "curve2flood_args", "TW_MultFact", "Top width multiplier", "float",
                      tooltip="Multiplies every top width (TW_MultFact).")
        self._add_arg(group, "curve2flood_args", "TopWidthPlausibleLimit", "Widest top width believed (m)", "float",
                      tooltip="TopWidthPlausibleLimit.")
        self._add_arg_table(group, "curve2flood_args", "More Curve2Flood settings",
                            "Other Curve2Flood settings for the flood maps, by their keys in its input file (see "
                            "Curve2Flood's docs).")
        fldpln = self._is_fldpln
        solver = lambda: self.arg_fields[("fldpln_library_args", "solver")][0].currentText()
        self.show_when(fldpln, "use_parquet", "fldpln_library_args.solver", "fldpln_library_args.parallel",
                       "curve2flood_args.FLDPLN_Min_Flood_Depth", "fldpln_library_args.fldmx",
                       "fldpln_library_args.spill_decay", "fldpln_library_args")
        self.show_when(lambda: fldpln() and solver() == "iterative", "fldpln_library_args.dh",
                       "fldpln_library_args.iterative_spill", "fldpln_library_args.fldmn")
        self.show_when(lambda: fldpln() and solver() == "exact", "fldpln_library_args.max_cells_per_segment")
        self.show_when(lambda: not fldpln(), "curve2flood_args.TW_MultFact", "curve2flood_args.TopWidthPlausibleLimit")
        self._indent("use_parquet", "fldpln_library_args.solver", "fldpln_library_args.dh",
                     "fldpln_library_args.iterative_spill", "fldpln_library_args.parallel", "fldpln_library_args.fldmn",
                     "curve2flood_args.FLDPLN_Min_Flood_Depth", "fldpln_library_args.fldmx",
                     "fldpln_library_args.spill_decay", "fldpln_library_args.max_cells_per_segment",
                     "fldpln_library_args")

        group = self._make_group(tab, "Outputs")
        self._add_checkbox(group, "make_depth_maps", "Make depth maps", _default("make_depth_maps"))
        self._add_checkbox(group, "make_velocity_maps", "Make velocity maps", _default("make_velocity_maps"))
        self._add_checkbox(group, "make_wse_maps", "Make water surface elevation maps", _default("make_wse_maps"))
        self._add_arg(group, "curve2flood_args", "Make_Output_GPKG", "Also write the flood extents as GeoPackages",
                      "bool")
        self._add_line(group, "floodmap_identifier", "Text added to the flood maps' names", _default("floodmap_identifier"))

    def _add_fist_tab(self):
        tab = self._make_tab("FIST")
        group = self._make_group(tab, "FIST")
        self._add_checkbox(group, "make_fist_inputs", "Make FIST inputs (water surface points) for each forecast or "
                                                      "user flow file", _default("make_fist_inputs"))
        self._add_note(group, "fist_note", "They are made from ARC's curve file (Make the curve file, on the ARC tab).")

    def _add_consequences_tab(self):
        tab = self._make_tab("Go Consequences")
        group = self._make_group(tab, "Go-Consequences")
        self._add_checkbox(group, "estimate_consequences", "Estimate the flood damage with Go-Consequences",
                           _default("estimate_consequences"))
        self._add_note(group, "consequences_note", "Needs Docker, and works in CONUS only. It turns the depth maps on.")

    def _add_run_tab(self):
        tab = self._make_tab("Run")

        group = self._make_group(tab, "NenCarta")
        self._add_checkbox(group, "overwrite", "Remake everything that already exists", _default("overwrite"))
        self._add_checkbox(group, "overwrite_floodmaps", "Remake flood maps that already exist",
                           _default("overwrite_floodmaps"))
        self._add_combo(group, "compression", "Raster compression", ["LZW", "DEFLATE", "ZSTD", "NONE"],
                        default=_default("compression"), editable=True)
        self._add_checkbox(group, "raise_errors_if_nothing_in_domain", "Stop when a DEM has no streams or data",
                           _default("raise_errors_if_nothing_in_domain"))

        group = self._make_group(tab, "Execution")
        self._add_checkbox(group, "parallel", "Process the DEMs in parallel", _default("parallel"))
        self._add_line(group, "num_workers", "Processes", "", parser="int", placeholder="Optional")
        self._add_checkbox(group, "profile", "Print how long each step took", _default("profile"))
        self._add_checkbox(group, "quiet", "Hide ARC's and Curve2Flood's progress", _default("quiet"))
        self._add_checkbox(group, "use_yaml", "Write ARC's and Curve2Flood's input files as YAML", _default("use_yaml"))
        self.show_when(lambda: self._on("parallel"), "num_workers")
        self._indent("num_workers")

        group = self._make_group(tab, "Output Folders and File Names")
        self._add_checkbox(group, "short_file_names", "Short file names: drop the <source>_<DEM>_ prefix",
                           _default("short_file_names"))
        folders = TemplateTable(("default",) + WORKSPACE_FOLDERS, _saved_dict("folder_paths", {}), tags=FOLDER_TAGS,
                                headers=("Folder", "Path template"), add_label="Add a folder")
        self._add_row(group, "Folders", folders, "folder_paths", "dict",
                      "Where each output folder goes. 'default' applies to every folder not listed. A relative path "
                      "is inside the output folder; tags are filled in for each DEM.")
        file_names = TemplateTable(FILE_BLURBS, _saved_dict("file_names", {}), headers=("File", "New name"),
                                   add_label="Rename a file", keep_empty=True)
        self._add_row(group, "File names", file_names, "file_names", "dict",
                      "New names for kinds of output files. An empty name leaves just what tells files of a kind "
                      "apart, e.g. the return period.")
        self.names_preview = QPlainTextEdit()
        self.names_preview.setReadOnly(True)
        self.names_preview.setFont(QFont("Consolas", 9))
        self.names_preview.setMinimumHeight(220)
        self._place(group, "names_preview", "Where the outputs go", self.names_preview)

    # ------------------------------------------------------------------------------------------------------------
    # What the form shows
    # ------------------------------------------------------------------------------------------------------------

    def _on(self, key):
        return self.input_fields[key].isChecked()

    def _source(self):
        return STREAMFLOW_SOURCE_LABELS.get(self.input_fields["streamflow_source"].currentText(), "")

    def _is_nwm(self):
        return self._source().upper().startswith("NWM")

    def _is_custom(self):
        return self._source() == str(StreamflowSource.CUSTOM)

    def _is_fldpln(self):
        return self.input_fields["mapper"].currentText() == Mapper.CURVE2FLOOD_FLDPLNPY.value

    def _floodmap_mode(self):
        return FLOODMAP_MODE_LABELS.get(self.input_fields["floodmap_mode"].currentText())

    def _downloads_dems(self):
        return self.choices["dem_mode"].value() == "folder" and self._on("use_warning_flags_to_download_dem")

    def _arg_number(self, dict_key, arg):
        try:
            return float(self.arg_fields[(dict_key, arg)][0].text())
        except ValueError:
            return 0.0

    def _arg_on(self, dict_key, arg):
        return self.arg_fields[(dict_key, arg)][0].isChecked()

    def _connect_changes(self):
        """Refresh what the form shows, and its previews, whenever a field changes."""
        widgets = list(self.input_fields.values()) + list(self.choices.values()) + \
            [widget for widget, _ in self.arg_fields.values()]
        for widget in widgets:
            if isinstance(widget, PathPicker):
                widget.line_edit.textChanged.connect(self._on_change)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._on_change)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._on_change)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self._on_change)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self._on_change)
            elif isinstance(widget, (ChoiceRow, DepthsWidget, TemplateTable)):
                widget.changed.connect(self._on_change)
            elif isinstance(widget, MultiFilePicker):
                widget.changed_signal.connect(self._on_change)
            elif isinstance(widget, DictTable):
                widget.table.itemChanged.connect(self._on_change)
            elif isinstance(widget, ReturnPeriodPicker):
                for checkbox in widget.checkboxes.values():
                    checkbox.toggled.connect(self._on_change)

    def _on_change(self, *_):
        self._refresh_visibility()
        self._preview_timer.start()

    def _refresh_visibility(self):
        hidden = set()
        for names, predicate in self.rules:
            try:
                shown = predicate()
            except Exception:  # noqa: BLE001
                shown = True

            if not shown:
                hidden.update(names)

        for name, widgets in self.rows.items():
            for widget in widgets:
                widget.setVisible(name not in hidden)
                
        self.hidden = hidden

        self.input_fields["specify_depths_for_bathy_mask"].show_cleaner(self._on("clean_dem"))
        if self._is_custom():
            self.rows["flow_note"][0].setText(
                "A custom source downloads nothing: give a flow statistics file, or a return period flows file "
                "(and a flow duration curve file for p_exceed flows). Map user flow files or return periods.")
            placeholders = ("Required", "Required")
        else:
            self.rows["flow_note"][0].setText(
                "NenCarta builds each reach's flow statistics from the streamflow source. A flow statistics file "
                "and flow duration curves of your own are optional.")
            placeholders = ("COMID (default for NWM)", "TOCOMID (default for NWM)") if self._is_nwm() else \
                ("LINKNO (default for GEOGLOWS)", "DSLINKNO (default for GEOGLOWS)")
        self.input_fields["stream_id_field"].setPlaceholderText(placeholders[0])
        self.input_fields["downstream_id_field"].setPlaceholderText(placeholders[1])

    # ------------------------------------------------------------------------------------------------------------
    # Reading the form
    # ------------------------------------------------------------------------------------------------------------

    def _parse_widget_value(self, key, widget):
        return _parse_value(widget, self.field_parsers.get(key, "text"))

    def _get_params(self):
        """The config the form shows: hidden fields and empty ones are left out, so they take their defaults."""
        params = {}
        for key, widget in self.input_fields.items():
            if key not in self.hidden:
                params[key] = self._parse_widget_value(key, widget)

        for dict_key in ARG_FIELDS:
            args = dict(params.get(dict_key) or {})
            for (owner, arg), (widget, parser) in self.arg_fields.items():
                if owner != dict_key or f"{owner}.{arg}" in self.hidden:
                    continue
                value = _parse_value(widget, parser)
                # Left out at the model's own default, where nencarta has no value of its own for it
                if value is None or value == "" or \
                        (arg not in ARG_DEFAULTS[dict_key] and value == _model_default(dict_key, arg)):
                    continue
                args[arg] = value
            params[dict_key] = args

        params["name"] = params.pop("watershed_name")
        if params.get("streamflow_source"):
            params["streamflow_source"] = STREAMFLOW_SOURCE_LABELS.get(params["streamflow_source"],
                                                                       params["streamflow_source"])
        if params.get("floodmap_mode"):
            params["floodmap_mode"] = FLOODMAP_MODE_LABELS.get(params["floodmap_mode"], params["floodmap_mode"])

        return {key: value for key, value in params.items()
                if not (value is None or value == "" or value == [] or value == {})}

    def _validate_params(self, params):
        """Check the form, then the config itself. Returns warnings that don't stop a run."""
        if not params.get("name"):
            raise ValueError("Watershed name is required.")
        if not params.get("output_dir"):
            raise ValueError("Output folder is required.")
        dem_mode = self.choices["dem_mode"].value()
        if dem_mode == "file" and not params.get("dem"):
            raise ValueError("Choose a DEM file.")
        if dem_mode == "folder" and not params.get("dem_dir"):
            raise ValueError("Choose a DEM folder.")
        if dem_mode == "bbox" and not params.get("bbox"):
            raise ValueError("Give the bounding box to cut the DEM to.")
        if (dem_mode == "bbox" or params.get("buffer")) and not params.get("source_dems"):
            raise ValueError("Add the source DEMs to cut the DEM from.")
        if not any(params.get(key) for key in ("flowline", "source_flowlines")):
            raise ValueError("Choose the stream network file(s).")
        if self._is_nwm() and not (params.get("nwm_api_key") or os.getenv("NWM_API_KEY")):
            raise ValueError("NWM streamflow sources need an NWM API key.")
        if params.get("floodmap_mode") == FloodMapMode.USER.value and not params.get("user_flow_files"):
            raise ValueError("Add the user flow files to map.")
        if params.get("floodmap_mode") == FloodMapMode.RETURN_PERIOD.value and not params.get("return_periods"):
            raise ValueError("Pick the return periods to map.")

        NencartaConfig(dict(params))

        warnings = []
        if self._is_fldpln() and self._arg_number("arc_args", "Low_Spot_Range") > 0:
            warnings.append("Low_Spot_Range is above 0 with the Curve2Flood-FLDPLNpy mapper: its cross sections "
                            "can move off the stream cells FLDPLN floods from.")
        return warnings

    # ------------------------------------------------------------------------------------------------------------
    # Previews
    # ------------------------------------------------------------------------------------------------------------

    def _refresh_previews(self):
        self.preview_configuration(write_log=False)
        self._refresh_names_preview()

    def _refresh_names_preview(self):
        """Where the outputs of the first DEM go, from a workspace made with the form's settings."""
        try:
            params = self._get_params()
            params.setdefault("output_dir", "output_folder")
            params.pop("mannings_text_file", None)  # the preview doesn't need the table to exist
            if not any(params.get(key) for key in ("flowline", "source_flowlines")):
                params["flowline"] = "streams.gpkg"
            dem = params.get("dem") or (str(Path(params["dem_dir"]) / "<DEM>.tif") if params.get("dem_dir") else None)
            if not dem and not params.get("bbox"):
                dem = "dem.tif"
                params["dem"] = dem
            workspace = Workspace(NencartaConfig(params), Path(dem) if dem else None)
            files = (workspace.assigned_dem, workspace.DEM_StrmShp, workspace.new_StrmShp_matched, workspace.LAND_File,
                     workspace.DEM_Reanalsyis_FlowFile, workspace.ARC_FileName_Bathy, workspace.VDT_File_Bathy,
                     workspace.bathy_water_mask, workspace.FS_BathyFile)
            lines = ["Folders:"] + [f"  {folder:<15} {workspace.folder_path(folder)}" for folder in WORKSPACE_FOLDERS]
            lines += ["", "Some of the files:"] + [f"  {path}" for path in files]
        except Exception as exc:  # noqa: BLE001
            lines = [f"The output paths can't be shown yet: {exc}"]
        self.names_preview.setPlainText("\n".join(lines))

    def preview_configuration(self, write_log=True):
        try:
            params = self._get_params()
            self.preview_text.setPlainText(json.dumps(params, indent=2, default=str))
            if write_log:
                self.log_message("[INFO] Configuration preview updated.")
        except Exception as exc:  # noqa: BLE001
            self.preview_text.setPlainText(f"Could not build configuration: {exc}")
            if write_log:
                self.log_message(f"[WARNING] Could not build configuration preview: {exc}")

    def validate_configuration(self):
        try:
            params = self._get_params()
            warnings = self._validate_params(params)
            self.preview_text.setPlainText(json.dumps(params, indent=2, default=str))
            self.save_settings()
            for warning in warnings:
                self.log_message(f"[WARNING] {warning}")
            self.log_message("[INFO] Configuration is valid.")
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Parameter error: {exc}")

    def start_simulation(self):
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.warning(self, "Running", "A simulation is already running.")
            return

        self.log_text.clear()
        self._bars, self._finished_bars, self._last_bar = {}, set(), None
        self.log_text.append("[INFO] Collecting parameters...")
        self.run_button.setEnabled(False)
        self.validate_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.run_button.setText("Running...")

        try:
            params = self._get_params()
            for warning in self._validate_params(params):
                self.log_text.append(f"[WARNING] {warning}")
            self.preview_text.setPlainText(json.dumps(params, indent=2, default=str))
            self.save_settings()
        except Exception as e:  # noqa: BLE001
            self.show_error(f"Parameter error: {e}")
            return

        self.worker_thread = WorkerThread(params, self.log_relay.message)
        self.worker_thread.finished_signal.connect(self.display_results)
        self.worker_thread.error_signal.connect(self.show_error)
        self.worker_thread.start()

    def log_message(self, message):
        """
        Add a message to the log. A tqdm progress bar stays on one line, rewritten as it updates, even with other
        messages logged in between; so does any line rewritten with a carriage return. Once a bar finishes, the
        next bar of the same name takes a new line.
        """
        message = str(message)
        if message == END_OF_REWRITTEN_LINE:
            bar = self._last_bar
            if bar is not None and (bar == "\r" or not _PROGRESS_COUNT.search(self._bar_text(bar))):
                self._finished_bars.add(bar)  # a bar without a total, or a rewritten line, ends with its line
            return
        
        rewritten = message.startswith("\r")
        text = _TERMINAL_ESCAPE.sub("", message.lstrip("\r")).rstrip()
        if not text:
            return

        progress = _progress_bar(text)
        bar, finished = progress if progress else ("\r" if rewritten else None, False)
        if bar is None:
            self.log_text.append(text)
        else:
            block = self.log_text.document().findBlockByNumber(self._bars.get(bar, -1))
            if bar in self._bars and block.isValid() and (bar not in self._finished_bars or finished):
                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                cursor.insertText(text)
            else:
                self.log_text.append(text)
                self._bars[bar] = self.log_text.document().blockCount() - 1
                self._finished_bars.discard(bar)
            self._last_bar = bar
            if finished:
                self._finished_bars.add(bar)

        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _bar_text(self, bar):
        block = self.log_text.document().findBlockByNumber(self._bars.get(bar, -1))
        return block.text() if block.isValid() else ""

    def display_results(self):
        self.log_message("[COMPLETED] Simulation finished.")
        self.run_button.setEnabled(True)
        self.validate_button.setEnabled(True)
        self.preview_button.setEnabled(True)
        self.run_button.setText("Start Simulation")

    def show_error(self, message):
        summary = str(message)
        details = None
        marker = "\n\nDETAILS:\n"
        if marker in summary:
            summary, details = summary.split(marker, 1)

        self.log_text.append(f"[ERROR] {summary}")

        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Critical)
        dlg.setWindowTitle("Simulation Error")
        dlg.setText("An error occurred.")
        dlg.setInformativeText(summary)
        if details:
            dlg.setDetailedText(details)
        dlg.exec_()

        self.run_button.setEnabled(True)
        self.validate_button.setEnabled(True)
        self.preview_button.setEnabled(True)
        self.run_button.setText("Start Simulation")

    # ------------------------------------------------------------------------------------------------------------
    # Remembering the form
    # ------------------------------------------------------------------------------------------------------------

    def save_settings(self, *_):
        """Remember every field as it is, hidden or not, and the form's own choices."""
        for key, widget in self.input_fields.items():
            if key == "disable_bathymetry":
                settings.setValue(key, not widget.isChecked())
            elif key == "use_specified_depth_for_bathy_mask":
                settings.setValue("gui/water_mask_mode", widget.value())
            elif isinstance(widget, DepthsWidget):
                settings.setValue(key, json.dumps(widget.state()))
            elif isinstance(widget, (DictTable, TemplateTable)) and key in ARG_FIELDS:
                args = widget.to_dict()
                for (owner, arg), (field, parser) in self.arg_fields.items():
                    if owner == key:
                        value = _parse_value(field, parser, strict=False)
                        if value is not None and value != "":
                            args[arg] = value
                settings.setValue(key, json.dumps(args))
            elif isinstance(widget, (DictTable, TemplateTable)):
                settings.setValue(key, json.dumps(widget.to_dict()))
            elif isinstance(widget, (MultiFilePicker, ReturnPeriodPicker)):
                settings.setValue(key, json.dumps(widget.values()))
            elif isinstance(widget, PathPicker):
                settings.setValue(key, widget.text())
            elif isinstance(widget, QLineEdit):
                settings.setValue(key, widget.text().strip())
            elif isinstance(widget, QCheckBox):
                settings.setValue(key, widget.isChecked())
            elif isinstance(widget, QComboBox):
                settings.setValue(key, widget.currentText().strip())
            elif isinstance(widget, QSpinBox):
                settings.setValue(key, widget.value())
        for name, widget in self.choices.items():
            if name in self.input_fields:
                continue
            settings.setValue(f"gui/{name}", widget.value() if isinstance(widget, ChoiceRow) else widget.isChecked())


def _parse_value(widget, parser, strict=True):
    """
    A field's value as its config key takes it. Text loses any quotes around it. With ``strict``, a number that
    can't be read raises; without, it is kept as text.
    """
    if isinstance(widget, QCheckBox):
        return not widget.isChecked() if parser == "inverted_bool" else widget.isChecked()
    if isinstance(widget, ChoiceRow):
        return widget.value() == "depth" if parser == "choice" else widget.value()
    if isinstance(widget, DepthsWidget):
        return widget.values()
    if isinstance(widget, (DictTable, TemplateTable)):
        return widget.to_dict()
    if isinstance(widget, (MultiFilePicker, ReturnPeriodPicker)):
        return widget.values()
    if isinstance(widget, QSpinBox):
        return widget.value()
    if isinstance(widget, PathPicker):
        value = widget.text()
    elif isinstance(widget, QLineEdit):
        value = strip_quotes(widget.text())
    elif isinstance(widget, QComboBox):
        value = strip_quotes(widget.currentText())
    else:
        return None

    if parser in {"text", "path"}:
        return value or None
    if parser == "str_list":
        return _split_list(value)
    if parser == "float_list":
        return _parse_float_list(value)
    if parser == "int_list":
        return _parse_int_list(value)
    if parser == "bbox":
        return _parse_bbox(value)
    if value == "":
        return None
    try:
        if parser == "int":
            return int(float(value))
        if parser == "float":
            return float(value)
    except ValueError:
        if strict:
            raise ValueError(f"'{value}' is not a number.")
    return value


def _model_default(dict_key, arg):
    """The model's own default for one of its settings, ARC's or Curve2Flood's (by its input-file key) or
    build_fldpln_library's argument; None if it has none."""
    if dict_key == "fldpln_library_args":
        from curve2flood import build_fldpln_library

        parameter = inspect.signature(build_fldpln_library).parameters.get(arg)
        return None if parameter is None or parameter.default is inspect.Parameter.empty else parameter.default

    if dict_key == "arc_args":
        from arc.config import Configs as Settings
    else:
        from curve2flood.config import Config as Settings
        
    field = next((field for field in dataclasses.fields(Settings) if field.name == arg.lower()), None)
    return None if field is None or field.default is dataclasses.MISSING else field.default


def _saved_dict(key, default):
    """A dictionary the form remembered, or ``default``."""
    saved = settings.value(key)
    if saved:
        try:
            data = json.loads(saved) if isinstance(saved, str) else saved
            if isinstance(data, dict):
                return data
        except (TypeError, json.JSONDecodeError):
            pass
    return dict(default)


def _saved_toggle(name, inferred):
    """A checkbox of the form's own as it was left, or ``inferred`` the first time."""
    if settings.value(f"gui/{name}") is None:
        return inferred
    return settings.value(f"gui/{name}", type=bool)


def _saved_choice(name, choices, inferred):
    """A choice of the form's own as it was left, or ``inferred`` the first time."""
    saved = settings.value(f"gui/{name}")
    return saved if saved in choices else inferred


def _saved_dem_mode():
    saved = settings.value("gui/dem_mode")
    if saved in DEM_MODES:
        return saved
    if _setting_text("dem_dir"):
        return "folder"
    if _setting_text("bbox") and not _setting_text("dem"):
        return "bbox"
    return "file"


def _saved_flowline_mode():
    saved = settings.value("gui/flowline_mode")
    if saved in FLOWLINE_MODES:
        return saved
    return "many" if _setting_list("source_flowlines") and not _setting_text("flowline") else "file"


def _saved_water_mask_mode():
    saved = settings.value("gui/water_mask_mode")
    if saved in WATER_MASK_MODES:
        return saved
    depth = settings.value("use_specified_depth_for_bathy_mask", _default("use_specified_depth_for_bathy_mask"),
                           type=bool)
    return "depth" if depth else "water"


def apply_theme(app):
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#f6f7f9"))
    palette.setColor(QPalette.WindowText, QColor("#1f2933"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.AlternateBase, QColor("#f3f5f7"))
    palette.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipText, QColor("#1f2933"))
    palette.setColor(QPalette.Text, QColor("#1f2933"))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor("#1f2933"))
    palette.setColor(QPalette.BrightText, QColor("#b42318"))
    palette.setColor(QPalette.Link, QColor("#2563eb"))
    palette.setColor(QPalette.Highlight, QColor("#2563eb"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    app.setStyleSheet(
        """
        QWidget {
            font-family: Segoe UI, Arial, sans-serif;
            font-size: 10pt;
            color: #1f2933;
        }
        QMainWindow, QWidget#centralWidget {
            background: #f6f7f9;
        }
        QLabel#AppTitle {
            font-size: 22pt;
            font-weight: 700;
            color: #111827;
        }
        QLabel#AppSubtitle {
            color: #5f6b7a;
            font-size: 10pt;
        }
        QGroupBox {
            background: #ffffff;
            border: 1px solid #d9dee7;
            border-radius: 8px;
            margin-top: 12px;
            font-weight: 600;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 4px;
        }
        QListWidget {
            background: #ffffff;
            border: 1px solid #cfd6df;
            border-radius: 6px;
            padding: 4px;
        }
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QTableWidget {
            background: #ffffff;
            border: 1px solid #cfd6df;
            border-radius: 6px;
            padding: 6px;
            selection-background-color: #2563eb;
        }
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
            border: 1px solid #2563eb;
        }
        QTextEdit {
            background: #111827;
            color: #e5e7eb;
            border: 1px solid #1f2937;
        }
        QPushButton {
            background: #ffffff;
            border: 1px solid #cfd6df;
            border-radius: 6px;
            padding: 7px 12px;
        }
        QPushButton:hover {
            background: #f3f5f7;
        }
        QPushButton#PrimaryButton {
            background: #2563eb;
            color: #ffffff;
            border: 1px solid #2563eb;
            font-weight: 600;
            padding: 8px 18px;
        }
        QPushButton#PrimaryButton:hover {
            background: #1d4ed8;
        }
        QPushButton:disabled {
            color: #8a95a3;
            background: #e7ebf0;
            border-color: #d7dde5;
        }
        QTabWidget::pane {
            border: 1px solid #d9dee7;
            border-radius: 8px;
            background: #ffffff;
        }
        QTabBar::tab {
            background: #e7ebf0;
            border: 1px solid #d9dee7;
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            padding: 8px 12px;
            margin-right: 2px;
        }
        QTabBar::tab:selected {
            background: #ffffff;
            color: #111827;
        }
        QScrollArea {
            border: none;
            background: transparent;
        }
        QCheckBox, QRadioButton {
            spacing: 8px;
        }
        QLabel#Note {
            color: #5f6b7a;
        }
        QPlainTextEdit, QTextEdit {
            font-family: Consolas, "Courier New", monospace;
            font-size: 9pt;
        }
        QLabel#WarningNote {
            color: #9a3412;
            background: #fff7ed;
            border: 1px solid #fed7aa;
            border-radius: 6px;
            padding: 6px;
        }
        QTableWidget {
            gridline-color: #d9dee7;
        }
        QHeaderView::section {
            background: #f3f5f7;
            border: 0;
            border-bottom: 1px solid #d9dee7;
            padding: 6px;
            font-weight: 600;
        }
        """
    )


def run_gui():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    apply_theme(app)
    window = FloodSimulationGUI()
    app.aboutToQuit.connect(window.save_settings)
    window.show()
    sys.exit(app.exec_())
