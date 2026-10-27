# built-in imports
import io
import json
import logging
import os
import sys
import traceback
from pathlib import Path

# third-party imports
from PyQt5.QtCore import QSettings, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPalette, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
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
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# local imports
from nencarta.core.configs import NencartaConfig
from nencarta.core.defaults import (
    DEFAULT_BATHY_ARGS,
    DEFAULT_CONFIG,
    DEFAULT_FLOODMAP_ARGS,
    FLOW_FIELD_OPTIONS,
    VALID_RETURN_PERIODS,
)
from nencarta.core.enumerations import FloodMapMode, Mapper, StreamflowSource
from nencarta.logger import LOG


settings = QSettings("NenCarta", "FloodSimulationGUI")

STREAMFLOW_SOURCE_LABELS = {
    str(StreamflowSource.GEOGLOWS): str(StreamflowSource.GEOGLOWS),
    "NWM Short Range": str(StreamflowSource.NWM_SHORT_RANGE),
    "NWM Medium Range": str(StreamflowSource.NWM_MEDIUM_RANGE),
    "NWM Long Range": str(StreamflowSource.NWM_LONG_RANGE),
}


def _streamflow_label(value):
    for label, source in STREAMFLOW_SOURCE_LABELS.items():
        if value in {label, source}:
            return label
    return str(StreamflowSource.GEOGLOWS)


class QtLogStream(io.TextIOBase):
    def __init__(self, qt_signal):
        super().__init__()
        self.qt_signal = qt_signal
        self._buffer = ""
        self._replace_next = False

    def write(self, text):
        if not text:
            return 0

        for char in text:
            if char == "\r":
                if self._buffer.strip():
                    self.qt_signal.emit(("\r" if self._replace_next else "") + self._buffer)
                self._buffer = ""
                self._replace_next = True
            elif char == "\n":
                if self._buffer.strip():
                    self.qt_signal.emit(("\r" if self._replace_next else "") + self._buffer)
                self._buffer = ""
                self._replace_next = False
            else:
                self._buffer += char

        if self._replace_next and self._buffer.strip():
            self.qt_signal.emit("\r" + self._buffer)
            self._buffer = ""
        return len(text)

    def flush(self):
        if self._buffer.strip():
            self.qt_signal.emit(("\r" if self._replace_next else "") + self._buffer)
        self._buffer = ""
        self._replace_next = False


class QtLogHandler(logging.Handler):
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        try:
            self.signal.emit(self.format(record))
        except Exception:
            self.handleError(record)


class WorkerThread(QThread):
    finished_signal = pyqtSignal(str)
    log_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = dict(params)

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
        except Exception:
            pass

        summary_parts = [context] if context else []
        summary_parts.append(f"{exc_type}: {exc_msg}".strip())
        if location:
            summary_parts.append(f"Location: {location}")
        if code_line:
            summary_parts.append(f"Code: {code_line}")
        return "\n".join(summary_parts), traceback.format_exc()

    def run(self):
        qt_handler = QtLogHandler(self.log_signal)
        qt_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        LOG.addHandler(qt_handler)
        LOG.setLevel(logging.INFO)

        old_stdout, old_stderr = sys.stdout, sys.stderr
        stream = QtLogStream(self.log_signal)

        try:
            sys.stdout = stream
            sys.stderr = stream
            from nencarta.main import process_watershed

            LOG.info("Running nencarta.process_watershed()")
            LOG.info(json.dumps(self.params, indent=2, default=str))
            process_watershed(dict(self.params))
            self.finished_signal.emit(self.params["name"])
        except Exception as e:
            summary, details = self._format_exception(e, context="Error running simulation")
            LOG.error(summary.replace("\n", " | "))
            self.error_signal.emit(summary + "\n\nDETAILS:\n" + details)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            LOG.removeHandler(qt_handler)


class PathPicker(QWidget):
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
        current = self.line_edit.text().strip()
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
        return self.line_edit.text()


class MultiFilePicker(QWidget):
    changed_signal = pyqtSignal()

    def __init__(self, default_paths=None, dialog_title="Select Files", file_filter="All Files (*)"):
        super().__init__()
        self.dialog_title = dialog_title
        self.file_filter = file_filter
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_widget.setMinimumHeight(96)

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

    def add_value(self, value):
        value = str(value).strip()
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
            values = _split_list(values)
        for value in values:
            self.add_value(value)
        if self.values() != old_values:
            self.changed_signal.emit()

    def values(self):
        return [self.list_widget.item(row).text() for row in range(self.list_widget.count())]


class ReturnPeriodPicker(QWidget):
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
    except Exception:
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


def _split_list(value):
    text = str(value or "").replace(",", "\n")
    return [part.strip() for part in text.splitlines() if part.strip()]


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
    return text.startswith("\\\\") or text.startswith("//")


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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NenCarta")
        icon_path = Path(__file__).with_name("images") / "header_logo.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(1320, 820)
        self.input_fields = {}
        self.field_parsers = {}
        self.worker_thread = None
        self._last_log_message_was_progress = False
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
        self.tabs.setMinimumWidth(560)
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
        self._wire_visibility()
        self.preview_configuration(write_log=False)
        self.log_text.setText("[INFO] Configure inputs, then start the simulation.\n")

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

    def _add_row(self, layout, label, widget, key, parser="text", tooltip=None):
        row = layout.rowCount()
        label_widget = QLabel(label)
        label_widget.setWordWrap(True)
        if tooltip:
            label_widget.setToolTip(tooltip)
            widget.setToolTip(tooltip)
        layout.addWidget(label_widget, row, 0)
        layout.addWidget(widget, row, 1)
        self.input_fields[key] = widget
        self.field_parsers[key] = parser
        return widget

    def _add_checkbox(self, layout, key, label, default=False, tooltip=None):
        widget = QCheckBox(label)
        widget.setChecked(settings.value(key, default, type=bool))
        if tooltip:
            widget.setToolTip(tooltip)
        row = layout.rowCount()
        layout.addWidget(widget, row, 0, 1, 2)
        self.input_fields[key] = widget
        self.field_parsers[key] = "bool"
        return widget

    def _add_line(self, layout, key, label, default="", parser="text", placeholder="", tooltip=None):
        value = _setting_text(key, default)
        widget = QLineEdit(value)
        widget.setPlaceholderText(placeholder)
        return self._add_row(layout, label, widget, key, parser, tooltip)

    def _add_combo(self, layout, key, label, items, default=None, editable=False, parser="text", tooltip=None):
        saved = settings.value(key, default if default is not None else "")
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
        widget = PathPicker(mode=mode, default_path=_setting_text(key, default), dialog_title=f"Select {label}", file_filter=file_filter)
        return self._add_row(layout, label, widget, key, "path", tooltip)

    def _add_multi_files(self, layout, key, label, default=None, file_filter="All Files (*)", tooltip=None):
        widget = MultiFilePicker(
            _setting_list(key, default),
            dialog_title=f"Select {label}",
            file_filter=file_filter,
        )
        return self._add_row(layout, label, widget, key, "str_list", tooltip)

    def _add_return_periods(self, layout, key, label, default=None, tooltip=None):
        widget = ReturnPeriodPicker(_setting_list(key, default))
        return self._add_row(layout, label, widget, key, "int_list", tooltip)

    def _add_dict(self, layout, key, label, default):
        saved = settings.value(key)
        data = default
        if saved:
            try:
                data = json.loads(saved)
            except (TypeError, json.JSONDecodeError):
                data = default
        widget = DictTable(data, defaults=default)
        widget.setMinimumHeight(260)
        return self._add_row(layout, label, widget, key, "dict")

    def _add_input_tabs(self):
        inputs_tab = self._make_tab("Inputs")
        run_tab = self._make_tab("Run")
        hydro_tab = self._make_tab("Hydro")
        bathy_tab = self._make_tab("Bathymetry")
        outputs_tab = self._make_tab("Outputs")
        advanced_tab = self._make_tab("Advanced")

        raster_files = "Raster Files (*.tif *.tiff *.vrt *.img);;All Files (*)"
        source_files = "Raster/Vector Files (*.tif *.tiff *.vrt *.img *.gpkg *.shp *.gdb *.parquet *.geoparquet);;All Files (*)"
        vector_files = "Vector Files (*.gpkg *.shp *.gdb *.parquet *.geoparquet *.geojson *.json);;All Files (*)"

        group = self._make_group(inputs_tab, "Required")
        self._add_line(group, "watershed_name", "Watershed name", settings.value("name", "example"), placeholder="example")
        self._add_path(group, "output_dir", "Output directory", "", mode="directory")

        group = self._make_group(inputs_tab, "DEM Source")
        self._add_path(group, "dem_dir", "DEM directory", "", mode="directory")
        self._add_path(group, "dem", "DEM file", "", file_filter=raster_files)
        self._add_multi_files(group, "source_dems", "Source DEM files", _default("source_dems"), source_files)
        self._add_line(group, "bbox", "Bounding box", "", parser="bbox", placeholder="minx, miny, maxx, maxy")
        self._add_checkbox(group, "buffer", "Buffer DEM from source DEMs", _default("buffer"))
        self._add_line(group, "buffer_distance", "Buffer distance", _default("buffer_distance"), parser="float")
        self._add_checkbox(group, "use_vrt", "Create assigned DEM as VRT", _default("use_vrt"))
        self._add_checkbox(group, "use_warning_flags_to_download_dem", "Use warning flags to download DEM", _default("use_warning_flags_to_download_dem"))
        self._add_line(group, "dem_filter", "DEM filter", _default("dem_filter"), placeholder="*.tif")

        group = self._make_group(inputs_tab, "Flowline Source")
        self._add_path(group, "flowline", "Flowline file", "", file_filter=vector_files)
        self._add_multi_files(group, "source_flowlines", "Source flowline files", _default("source_flowlines"), vector_files)

        group = self._make_group(run_tab, "Representative Cross Sections")
        self._add_checkbox(
            group, "representative_cross_section_creation", "Create Representative Cross Sections",
            _default("representative_cross_section_creation"),
            tooltip="Export ARC representative hydraulics and coefficients for each DEM; skip mapping except the optional DEM cleaner's initial mapper run.",
        )
        self._add_line(
            group, "stream_length_field", "Reach length column (meters)", _default("stream_length_field") or "",
            tooltip="Existing flowline column containing reach lengths in meters; required for representative cross sections.",
        )

        group = self._make_group(run_tab, "Core")
        self._add_combo(group, "mapper", "Mapper", Mapper.list_names(), default=_default("mapper"))
        saved_streamflow_source = _streamflow_label(settings.value("streamflow_source", _default("streamflow_source")))
        self.streamflow_source = self._add_combo(group, "streamflow_source", "Streamflow source", STREAMFLOW_SOURCE_LABELS.keys(), default=saved_streamflow_source)
        self.nwm_api_key = self._add_line(group, "nwm_api_key", "NWM API key", "", placeholder="Required for NWM sources")
        self.nwm_api_key_label = group.itemAtPosition(group.rowCount() - 1, 0).widget()
        self._add_combo(group, "floodmap_mode", "Floodmap mode", [mode.value for mode in FloodMapMode], default=_default("floodmap_mode"))
        self._add_multi_files(group, "user_flow_files", "User flow files", _default("user_flow_files"), "CSV Files (*.csv);;All Files (*)")
        self._add_return_periods(group, "return_periods", "Return periods", _default("return_periods"))
        self._add_path(group, "reanalysis_file", "Reanalysis file", "", file_filter="CSV Files (*.csv);;All Files (*)")

        group = self._make_group(run_tab, "Forecast")
        self._add_line(group, "forensic_forecast_date", "Forensic forecast date", "", placeholder="YYYYMMDD")
        self._add_combo(group, "forensic_forecast_hour", "Forensic forecast hour", [""] + [f"{hour:02d}" for hour in range(24)], default="")
        self._add_spin(group, "age_of_forecast_days", "Forecast age days", _default("age_of_forecast_days"), 1, 365)
        self._add_combo(group, "geoglows_vpu", "GEOGLOWS VPU", ["", "704", "702", "703", "715", "714", "706", "713", "712", "709"], default="")

        group = self._make_group(run_tab, "Execution")
        self._add_checkbox(group, "parallel", "Run workspaces in parallel", _default("parallel"))
        self._add_line(group, "num_workers", "Worker count", "", parser="int", placeholder="Optional")
        self._add_checkbox(group, "profile", "Print profiling stats", _default("profile"))
        self._add_checkbox(group, "quiet", "Reduce model output", _default("quiet"))
        self._add_checkbox(group, "use_yaml", "Write mapper configs as YAML", _default("use_yaml"))

        group = self._make_group(hydro_tab, "DEM and Stream Network")
        self._add_checkbox(group, "clean_dem", "Clean DEM", _default("clean_dem"))
        self._add_checkbox(group, "overwrite", "Overwrite existing products", _default("overwrite"))
        self._add_checkbox(group, "move_stream_network_to_thalweg", "Move stream network to thalweg", _default("move_stream_network_to_thalweg"))
        self._add_checkbox(group, "burn_streams", "Burn streams into DEM", _default("burn_streams"))
        self._add_checkbox(group, "project_to_utm", "Project everything to UTM", _default("project_to_utm"))
        self._add_checkbox(group, "streams_as_parquet", "Write streams as parquet", _default("streams_as_parquet"))
        self._add_checkbox(group, "use_parquet", "Use parquet intermediates", _default("use_parquet"))
        self._add_line(group, "new_strm_threshold_km2", "New stream threshold km2", _default("new_strm_threshold_km2"), parser="float")
        self._add_line(group, "q_baseflow_threshold", "Baseflow threshold", "", parser="float")
        self._add_line(group, "slope_low_percentile", "Slope low percentile", _default("slope_low_percentile"), parser="float")
        self._add_line(group, "slope_high_percentile", "Slope high percentile", _default("slope_high_percentile"), parser="float")

        group = self._make_group(hydro_tab, "Stream and Lake Filters")
        self._add_line(group, "StrmOrder_Field", "Stream order field", "")
        self._add_line(group, "StrmOrder_Lower", "Stream order lower", "", parser="int")
        self._add_line(group, "StrmOrder_Upper", "Stream order upper", "", parser="int")
        self._add_path(group, "lake_filter_json", "Lake filter JSON", "", file_filter="JSON Files (*.json);;All Files (*)")
        self._add_path(group, "lakes", "Lakes file", "", file_filter=vector_files)

        group = self._make_group(hydro_tab, "Land Cover")
        self._add_multi_files(group, "land_cover_cache", "Land cover cache files", _default("land_cover_cache"), raster_files)
        self._add_line(group, "land_watervalue", "Land water value", _default("land_watervalue"), parser="int")
        self._add_checkbox(group, "flood_waterlc_and_strm_cells", "Flood water land-cover and stream cells", _default("flood_waterlc_and_strm_cells"))

        group = self._make_group(bathy_tab, "Bathymetry")
        self._add_checkbox(group, "disable_bathymetry", "Disable bathymetry", _default("disable_bathymetry"))
        self._add_checkbox(group, "bathy_use_banks", "Use bathymetry banks", _default("bathy_use_banks"))
        self._add_checkbox(group, "find_banks_based_on_landcover", "Find banks from land cover", _default("find_banks_based_on_landcover"))
        self._add_checkbox(group, "use_specified_depth_for_bathy_mask", "Use specified depths for bathymetry mask", _default("use_specified_depth_for_bathy_mask"))
        self._add_line(group, "specify_depths_for_bathy_mask", "Bathymetry mask depths", ", ".join(str(value) for value in _default("specify_depths_for_bathy_mask")), parser="float_list", placeholder="0.1 or 0.1, 0.3")
        self._add_combo(group, "specified_bathyflow_field", "Bathymetry flow field", FLOW_FIELD_OPTIONS, default=_default("specified_bathyflow_field"), editable=True)
        self._add_combo(group, "specified_highflow_field", "High flow field", FLOW_FIELD_OPTIONS, default=_default("specified_highflow_field"), editable=True)
        self._add_checkbox(group, "create_reach_average_curve_file", "Create reach-average curve file", _default("create_reach_average_curve_file"))
        self._add_checkbox(group, "use_dem_derived_channel_mask", "Use DEM-derived channel mask", _default("use_dem_derived_channel_mask"))
        self._add_checkbox(group, "use_power_laws_for_bathymetry", "Use power laws for bathymetry", _default("use_power_laws_for_bathymetry"))
        self._add_line(group, "area_m2_field", "Area m2 field", _default("area_m2_field"))
        self._add_line(group, "area_km2_field", "Area km2 field", _default("area_km2_field"))
        self._add_line(group, "coefficient_depth", "Depth coefficient", _default("coefficient_depth"), parser="float")
        self._add_line(group, "exponent_depth", "Depth exponent", _default("exponent_depth"), parser="float")
        self._add_line(group, "coefficient_width", "Width coefficient", _default("coefficient_width"), parser="float")
        self._add_line(group, "exponent_width", "Width exponent", _default("exponent_width"), parser="float")
        self._add_path(group, "mannings_text_file", "Manning's n text file", "", file_filter="Text Files (*.txt);;All Files (*)")

        group = self._make_group(bathy_tab, "Bathymetry Arguments")
        self._add_dict(group, "bathy_args", "Arguments", DEFAULT_BATHY_ARGS)

        group = self._make_group(outputs_tab, "Products")
        self._add_checkbox(group, "make_fist_inputs", "Make FIST inputs", _default("make_fist_inputs"))
        self._add_checkbox(group, "make_vdt", "Make VDT database", _default("make_vdt"))
        self._add_checkbox(group, "make_curvefile", "Make curve file", _default("make_curvefile"))
        self._add_checkbox(group, "make_ap_database", "Make AP database", _default("make_ap_database"))
        self._add_checkbox(group, "make_cross_section_file", "Make cross-section file", _default("make_cross_section_file"))
        self._add_checkbox(group, "make_depth_maps", "Make depth maps", _default("make_depth_maps"))
        self._add_checkbox(group, "make_velocity_maps", "Make velocity maps", _default("make_velocity_maps"))
        self._add_checkbox(group, "make_wse_maps", "Make WSE maps", _default("make_wse_maps"))
        self._add_checkbox(group, "estimate_consequences", "Estimate consequences", _default("estimate_consequences"))

        group = self._make_group(outputs_tab, "Flood Maps")
        self._add_checkbox(group, "overwrite_floodmaps", "Overwrite flood maps", _default("overwrite_floodmaps"))
        self._add_checkbox(group, "remove_old_forecast_files", "Remove old forecast files", _default("remove_old_forecast_files"))
        self._add_line(group, "floodmap_identifier", "Flood map identifier", _default("floodmap_identifier"))
        self._add_combo(group, "vdt_file_extension", "VDT file extension", ["txt", "csv", "parquet"], default=_default("vdt_file_extension"))

        group = self._make_group(outputs_tab, "Floodmap Arguments")
        self._add_dict(group, "floodmap_args", "Arguments", DEFAULT_FLOODMAP_ARGS)

        group = self._make_group(advanced_tab, "FLDPLN")
        self._add_line(group, "fldpln_dh", "Depth interval", _default("fldpln_dh"), parser="float")
        self._add_line(group, "fldpln_min_depth", "Minimum depth", _default("fldpln_min_depth"), parser="float")
        self._add_line(group, "fldpln_max_depth", "Maximum depth", _default("fldpln_max_depth"), parser="float")
        self._add_line(group, "fldpln_max_wse_rise", "Maximum WSE rise", _default("fldpln_max_wse_rise"), parser="float")
        self._add_checkbox(group, "fldpln_keep_spilling", "Keep spilling cells", _default("fldpln_keep_spilling"))
        self._add_checkbox(group, "fldpln_parallel", "Run FLDPLN in parallel", _default("fldpln_parallel"))

        group = self._make_group(advanced_tab, "Runtime and Output")
        self._add_combo(group, "compression", "Raster compression", ["LZW", "DEFLATE", "ZSTD", "NONE"], default=_default("compression"), editable=True)
        self._add_checkbox(group, "raise_errors_if_nothing_in_domain", "Raise if nothing is in domain", _default("raise_errors_if_nothing_in_domain"))
        self._add_line(group, "exclude", "Exclude stream IDs", "", parser="int_list", placeholder="123, 456")

    def _wire_visibility(self):
        def toggle_nwm_api_key(value):
            source = STREAMFLOW_SOURCE_LABELS.get(value, value)
            is_nwm = str(source).upper().startswith("NWM")
            self.nwm_api_key.setVisible(is_nwm)
            self.nwm_api_key_label.setVisible(is_nwm)

        self.streamflow_source.currentTextChanged.connect(toggle_nwm_api_key)
        toggle_nwm_api_key(self.streamflow_source.currentText())

    def _parse_widget_value(self, key, widget):
        parser = self.field_parsers.get(key, "text")
        if isinstance(widget, PathPicker):
            value = widget.text().strip()
        elif isinstance(widget, QLineEdit):
            value = widget.text().strip()
        elif isinstance(widget, QPlainTextEdit):
            value = widget.toPlainText().strip()
        elif isinstance(widget, QCheckBox):
            return widget.isChecked()
        elif isinstance(widget, QComboBox):
            value = widget.currentText().strip()
        elif isinstance(widget, QSpinBox):
            return widget.value()
        elif isinstance(widget, DictTable):
            return widget.to_dict()
        elif isinstance(widget, MultiFilePicker):
            return widget.values()
        elif isinstance(widget, ReturnPeriodPicker):
            return widget.values()
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
        if parser == "int":
            return int(float(value))
        if parser == "float":
            return float(value)
        if parser == "bool":
            return bool(value)
        return value

    def _get_params(self):
        params = {}
        for key, widget in self.input_fields.items():
            params[key] = self._parse_widget_value(key, widget)

        params["name"] = params.pop("watershed_name")
        streamflow_label = params.get("streamflow_source")
        if streamflow_label:
            params["streamflow_source"] = STREAMFLOW_SOURCE_LABELS.get(streamflow_label, streamflow_label)

        clean_params = {}
        for key, value in params.items():
            if value is None or value == "" or value == []:
                continue
            clean_params[key] = value
        return clean_params

    def _validate_params(self, params):
        if not params.get("name"):
            raise ValueError("Watershed name is required.")
        if not params.get("output_dir"):
            raise ValueError("Output directory is required.")
        if not any(params.get(key) for key in ("flowline", "source_flowlines")):
            raise ValueError("Provide a flowline file or source flowline files.")
        if not any(params.get(key) for key in ("dem", "dem_dir", "source_dems")):
            raise ValueError("Provide a DEM file, DEM directory, or source DEM files.")
        if params.get("streamflow_source", "").upper().startswith("NWM") and not params.get("nwm_api_key"):
            raise ValueError("NWM API key is required for NWM streamflow sources.")
        if not params.get("representative_cross_section_creation") and params.get("floodmap_mode") == FloodMapMode.USER.value and not params.get("user_flow_files"):
            raise ValueError("User flow files are required when floodmap mode is user.")
        if not params.get("representative_cross_section_creation") and params.get("floodmap_mode") == FloodMapMode.RETURN_PERIOD.value and not params.get("return_periods"):
            raise ValueError("Return periods are required when floodmap mode is return_period.")

        NencartaConfig(dict(params))

    def preview_configuration(self, write_log=True):
        try:
            params = self._get_params()
            self.preview_text.setPlainText(json.dumps(params, indent=2, default=str))
            if write_log:
                self.log_message("[INFO] Configuration preview updated.")
        except Exception as exc:
            self.preview_text.setPlainText(f"Could not build configuration: {exc}")
            if write_log:
                self.log_message(f"[WARNING] Could not build configuration preview: {exc}")

    def validate_configuration(self):
        try:
            params = self._get_params()
            self._validate_params(params)
            self.preview_text.setPlainText(json.dumps(params, indent=2, default=str))
            self.save_settings(params)
            self.log_message("[INFO] Configuration is valid.")
        except Exception as exc:
            self.show_error(f"Parameter error: {exc}")

    def start_simulation(self):
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.warning(self, "Running", "A simulation is already running.")
            return

        self.log_text.clear()
        self.log_text.append("[INFO] Collecting parameters...")
        self.run_button.setEnabled(False)
        self.validate_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.run_button.setText("Running...")

        try:
            params = self._get_params()
            self._validate_params(params)
            if params.get("dem") and params.get("dem_dir"):
                self.log_text.append(
                    "[WARNING] Both DEM directory and DEM file are set. "
                    "process_watershed uses the DEM directory first."
                )
            self.preview_text.setPlainText(json.dumps(params, indent=2, default=str))
            self.save_settings(params)
        except Exception as e:
            self.show_error(f"Parameter error: {e}")
            return

        self.worker_thread = WorkerThread(params)
        self.worker_thread.finished_signal.connect(self.display_results)
        self.worker_thread.log_signal.connect(self.log_message)
        self.worker_thread.error_signal.connect(self.show_error)
        self.worker_thread.start()

    def log_message(self, message):
        message = str(message)
        if message.startswith("\r"):
            if self._last_log_message_was_progress:
                cursor = self.log_text.textCursor()
                cursor.movePosition(QTextCursor.End)
                cursor.select(QTextCursor.LineUnderCursor)
                cursor.removeSelectedText()
                cursor.insertText(message[1:])
            else:
                self.log_text.append(message[1:])
            self._last_log_message_was_progress = True
        else:
            self.log_text.append(message)
            self._last_log_message_was_progress = False
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def display_results(self, watershed_name: str):
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

    def save_settings(self, params=None):
        if params is None:
            for key, widget in self.input_fields.items():
                setting_key = "watershed_name" if key == "watershed_name" else key
                if isinstance(widget, PathPicker):
                    settings.setValue(setting_key, widget.text().strip())
                elif isinstance(widget, QLineEdit):
                    settings.setValue(setting_key, widget.text().strip())
                elif isinstance(widget, QPlainTextEdit):
                    settings.setValue(setting_key, widget.toPlainText().strip())
                elif isinstance(widget, QCheckBox):
                    settings.setValue(setting_key, widget.isChecked())
                elif isinstance(widget, QComboBox):
                    settings.setValue(setting_key, widget.currentText().strip())
                elif isinstance(widget, QSpinBox):
                    settings.setValue(setting_key, widget.value())
                elif isinstance(widget, DictTable):
                    settings.setValue(setting_key, json.dumps(widget.to_dict()))
                elif isinstance(widget, MultiFilePicker):
                    settings.setValue(setting_key, json.dumps(widget.values()))
                elif isinstance(widget, ReturnPeriodPicker):
                    settings.setValue(setting_key, json.dumps(widget.values()))
            return

        for key, value in params.items():
            if key == "name":
                settings.setValue("watershed_name", value)
            elif key == "streamflow_source":
                settings.setValue(key, _streamflow_label(value))
            elif isinstance(value, (dict, list)):
                settings.setValue(key, json.dumps(value))
            else:
                settings.setValue(key, value)


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
        QCheckBox {
            spacing: 8px;
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
