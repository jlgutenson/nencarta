# built-in imports
import io
import sys
import os
import logging
import traceback

# third-party imports
import json          # <-- add this
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLineEdit, QLabel, QPushButton, QCheckBox, QComboBox,
    QTextEdit, QGroupBox, QSpinBox, QMessageBox, QScrollArea, QLabel,
    QFileDialog, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QSizePolicy, QHeaderView
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QFont, QColor, QPalette, QIcon

# local imports
from . import main as flood_main
from .timer import Timer
from .logger import LOG

FLOW_FIELD_OPTIONS = (
    [f"p_exceed_{i}" for i in [0] + list(range(5, 101, 5))]
    + ["p_exceed_0_premium"]
    + ["rp2", "rp5", "rp10", "rp25", "rp50", "rp100", "rp100_premium"]
)

STREAMFLOW_SOURCE_LABELS = {
    "GEOGLOWS": "GEOGLOWS",
    "NWM Short Range": "NWM_short_range",
    "NWM Medium Range": "NWM_medium_range",
    "NWM Long Range": "NWM_long_range",
}

# Map of output keys to friendly names (from main.py output structure)
SIMULATION_TIMES_MAP = {
    'arc_initial_simulation_time': "ARC Initial Flood",
    'curve2flood_initial_simulation_time': "Curve2Flood Initial Flood",
    'floodpsreaderpy_initial_simulation_time': "FloodSpreaderPy Initial Flood",
    'dem_cleaner_simulation_time': "DEM Cleaner",
    'arc_bathy_simulation_time': "ARC Bathymetry",
    'curve2flood_bathy_simulation_time': "Curve2Flood Bathymetry",
    'floodpsreaderpy_bathy_simulation_time': "FloodSpreaderPy Bathymetry",
    'curve2flood_forecast_simulation_time': "Curve2Flood Forecast",
    'floodpsreaderpy_forecast_simulation_time': "FloodSpreaderPy Forecast",
    'geojson_forecast_simulation_time': "GeoJSON Forecast (FIST)",
    'go_consequences_simulation_time': "Go-Consequences Estimation",
}

settings = QSettings("NenCarta", "FloodSimulationGUI")

class QtLogStream(io.TextIOBase):
    """
    File-like object that sends anything written to it into a Qt signal.
    Used to capture prints from process_json_input_serial into the GUI log.
    """
    def __init__(self, qt_signal):
        super().__init__()
        self.qt_signal = qt_signal
        self._buffer = ""

    def write(self, text):
        if not text:
            return 0

        # Accumulate into a buffer and emit full lines
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self.qt_signal.emit(line)
        return len(text)

    def flush(self):
        # Flush any remaining partial line
        if self._buffer.strip():
            self.qt_signal.emit(self._buffer)
        self._buffer = ""

class QtLogHandler(logging.Handler):
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        try:
            msg = self.format(record)
            self.signal.emit(msg)
        except Exception:
            self.handleError(record)


# --- WORKER THREAD ---

class WorkerThread(QThread):
    """Worker thread to run main.py via its JSON interface without blocking the GUI."""
    finished_signal = pyqtSignal(str, Timer)
    log_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    # --- helpers -------------------------------------------------------------

    def _build_watershed_dict(self):
        """
        Convert flat GUI params into the watershed dictionary that main.py
        expects inside the JSON 'watersheds' list.
        Mirrors the structure used in process_json_input_serial().
        """
        params = self.params

        # Parse depths from comma-separated text into floats
        if params.get("specify_depths_for_bathy_mask"):
            params["specify_depths_for_bathy_mask"] = [
                float(d.strip())
                for d in params["specify_depths_for_bathy_mask"].split(",")
                if d.strip()
            ]
        else:
            params["specify_depths_for_bathy_mask"] = []

        params["name"] = params["watershed_name"]

        params["user_flow_files"] = [line.strip() for line in params.get("user_flow_files","").splitlines() if line.strip()]

        return params

    def _write_json_input(self, watershed_dict):
        """
        Write a JSON file in the format expected by main.py's 'json' subcommand:
        {
          "parallel": false,
          "num_workers": 1,
          "watersheds": [ { ... } ]
        }
        """
        output_dir = watershed_dict["output_dir"]
        watershed_name = watershed_dict["name"]

        os.makedirs(output_dir, exist_ok=True)

        json_path = os.path.join(
            output_dir,
            f"{watershed_name}_input.json"
        )

        payload = {
            "parallel": False,     # you can expose these via the GUI later if desired
            "num_workers": 1,
            "watersheds": [watershed_dict],
        }

        with open(json_path, "w") as f:
            json.dump(payload, f, indent=2)

        return json_path

    @staticmethod
    def _format_exception(e: Exception, context: str = ""):
        """
        Return (summary, details) strings for an exception, including location + traceback.
        summary: short, human-readable error + location + failing line (best effort)
        details: full traceback (for expandable details pane)
        """
        exc_type = type(e).__name__
        exc_msg = str(e)

        # Best-effort extraction of where the exception occurred
        tb = e.__traceback__
        location = ""
        code_line = ""
        try:
            frames = traceback.extract_tb(tb)
            if frames:
                last = frames[-1]
                location = f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}()"
                if last.line:
                    code_line = last.line.strip()
        except Exception:
            pass

        summary_parts = []
        if context:
            summary_parts.append(context)
        summary_parts.append(f"{exc_type}: {exc_msg}".strip())
        if location:
            summary_parts.append(f"Location: {location}")
        if code_line:
            summary_parts.append(f"Code: {code_line}")
        summary = "\n".join(summary_parts)

        details = traceback.format_exc()
        return summary, details


    # --- main worker entry point ---------------------------------------------

    def run(self):
        qt_handler = QtLogHandler(self.log_signal)
        qt_handler.setFormatter(logging.Formatter(
            "[%(levelname)s] %(message)s"
        ))
        LOG.addHandler(qt_handler)
        LOG.setLevel(logging.INFO)

        # --- redirect stdout/stderr so all prints go into the GUI ---
        old_stdout, old_stderr = sys.stdout, sys.stderr
        stream = QtLogStream(self.log_signal)

        try:
            sys.stdout = stream
            sys.stderr = stream

            # 1) Build watershed dict from GUI parameters
            watershed_dict = self._build_watershed_dict()

            # 2) Write JSON input file
            json_file = self._write_json_input(watershed_dict)
            LOG.info("JSON input file created for main.py:")
            LOG.info(f"   {json_file}")

            # 3) Direct call instead of subprocess
            LOG.info("Running main.process_json_input_serial() ...")
            result = flood_main.process_json_input_serial(json_file)

            if isinstance(result, Timer):
                self.finished_signal.emit(watershed_dict['name'], result)
            else:
                self.finished_signal.emit(watershed_dict['name'], None)

        except Exception as e:
            context = "Error running simulation"
            summary, details = self._format_exception(e, context=context)

            # Keep log readable (single-line-ish summary)
            LOG.error(summary.replace("\n", " | "))

            # Send both summary + full traceback to the GUI
            self.error_signal.emit(summary + "\n\nDETAILS:\n" + details)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            LOG.removeHandler(qt_handler)

class DictTable(QWidget):
    def __init__(self):
        super().__init__()

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)

    def set_dict(self, data: dict):
        self.table.setRowCount(0)
        for key, value in data.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            
            key_item = QTableWidgetItem(str(key))
            key_item.setFlags(key_item.flags() & ~Qt.ItemIsEditable)

            self.table.setItem(row, 0, key_item)
            self.table.setItem(row, 1, QTableWidgetItem(str(value)))

    def to_dict(self) -> dict:
        result = {}
        for row in range(self.table.rowCount()):
            k = self.table.item(row, 0)
            v = self.table.item(row, 1)
            if k and v and k.text():
                result[k.text()] = v.text()
        return result
    
class DirectoryPicker(QWidget):
    def __init__(self, parent=None, default_path="", dialog_title="Select Directory"):
        super().__init__(parent)

        self.line_edit = QLineEdit(default_path)
        browse_btn = QPushButton("Browse…")

        browse_btn.clicked.connect(self._browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.line_edit)
        layout.addWidget(browse_btn)

        self.dialog_title = dialog_title

    def _browse(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            self.dialog_title,
            self.line_edit.text()
        )
        if directory:
            self.line_edit.setText(directory)

    def text(self):
        return self.line_edit.text()

class FilePicker(QWidget):
    def __init__(
        self,
        parent=None,
        default_path="",
        dialog_title="Select File",
        file_filter="All Files (*)"
    ):
        super().__init__(parent)

        self.line_edit = QLineEdit(default_path)
        browse_btn = QPushButton("Browse…")

        browse_btn.clicked.connect(self._browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.line_edit)
        layout.addWidget(browse_btn)

        self.dialog_title = dialog_title
        self.file_filter = file_filter

    def _browse(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            self.dialog_title,
            self.line_edit.text(),
            self.file_filter
        )
        if filename:
            self.line_edit.setText(filename)

    def text(self):
        return self.line_edit.text()

    def setText(self, value):
        self.line_edit.setText(value)

class FormSection:
    def __init__(self, title):
        self.group = QGroupBox(title)
        self.layout = QGridLayout(self.group)
        self.fields = {}

    def add(self, key, label, widget: QWidget):
        row = self.layout.rowCount()
        self.layout.addWidget(QLabel(label), row, 0)
        self.layout.addWidget(widget, row, 1)

        self.fields[key] = (
            widget.line_edit if hasattr(widget, "line_edit") else widget
        )

# --- GUI CLASS ---

class FloodSimulationGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NenCarta GUI")
        self.setWindowIcon(QIcon("images/nencarta_logo.png"))  # path to your logo

        self.setGeometry(100, 100, 1200, 800)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)

        self.main_layout = QHBoxLayout(self.central_widget)

        self.init_ui()
        self.worker_thread = None

    def init_ui(self):
        # --- Left Panel: Input Parameters ---
        self.input_area = QWidget()
        self.input_area.setFixedWidth(500)
        self.input_area.setFont(QFont("Arial", 10))
        self.input_layout = QVBoxLayout(self.input_area)
        self.input_layout.setSpacing(10)



        # --- Scroll area with input fields ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.input_grid = QGridLayout(scroll_content)
        self.input_grid.setSpacing(10)
        scroll.setWidget(scroll_content)

        self.input_layout.addWidget(scroll)
        self.main_layout.addWidget(self.input_area)

        # --- Right Panel: Control, Log, and Output ---
        self.output_area = QWidget()
        self.output_layout = QVBoxLayout(self.output_area)
        self.main_layout.addWidget(self.output_area)

        # 1. Control Button
        self.run_button = QPushButton("Start Simulation")
        self.run_button.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.run_button.setStyleSheet(
            "background-color: #4CAF50; color: white; padding: 10px; border-radius: 5px;"
        )
        self.run_button.clicked.connect(self.start_simulation)
        self.output_layout.addWidget(self.run_button)

        # 2. Log Output
        log_group = QGroupBox("Simulation Log")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Monospace", 9))
        self.log_text.setStyleSheet("background-color: #2e3436; color: #ffffff;")
        log_layout.addWidget(self.log_text)
        self.output_layout.addWidget(log_group, 3)  # Take 3/4 of space

        # 3. Results Output
        results_group = QGroupBox("Simulated Process Time Results (Minutes)")
        results_layout = QVBoxLayout(results_group)
        self.results_text = QLabel("Press 'Start Simulation' to see results.")
        self.results_text.setFont(QFont("Arial", 10))
        self.results_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        results_layout.addWidget(self.results_text)
        self.output_layout.addWidget(results_group, 1)  # Take 1/4 of space

        # --- Populate Input Fields ---
        self.input_fields = {}
        self._add_input_fields()

        # Initial instruction
        self.log_text.setText(
            "[GUIDANCE] Configure parameters on the left. The GUI will "
            "construct and log the call to your 'main.py' file and run the simulation.\n"
        )
        self.results_text.setText(
            "Timing results will appear here after the run is complete."
        )

    def _add_input_fields(self):
        """Adds all GUI controls based on the script's arguments."""
        row = 0

        # 1. Required Path Inputs (Mocked paths as defaults)
        group_req = QGroupBox("Required Inputs")
        group_req_layout = QGridLayout(group_req)
        
        i = 0
        self.watershed_name = QLineEdit(settings.value("watershed_name", "Yellowstone_DEM"))
        group_req_layout.addWidget(QLabel("Watershed Name"), i, 0); group_req_layout.addWidget(self.watershed_name, i, 1); self.input_fields['watershed_name'] = self.watershed_name; i+=1
        
        self.flowline = FilePicker(self, settings.value("flowline", "data/flowline.gpkg"), "Select Flowline File", "Geometry Files (*.gpkg *.shp *.gdb *.parquet *.geoparquet);;All Files (*)")
        group_req_layout.addWidget(QLabel("Flowline File"), i, 0)
        group_req_layout.addWidget(self.flowline, i, 1)
        self.input_fields["flowline"] = self.flowline.line_edit
        i += 1

        self.dem_dir = DirectoryPicker(self, settings.value("dem_dir", "data/dems"), "Select DEM Directory")
        group_req_layout.addWidget(QLabel("DEM Directory"), i, 0)
        group_req_layout.addWidget(self.dem_dir, i, 1)
        self.input_fields["dem_dir"] = self.dem_dir.line_edit
        i += 1

        self.output_dir = DirectoryPicker(self, settings.value("output_dir", "data/output"), "Select Output Directory")
        group_req_layout.addWidget(QLabel("Output Directory"), i, 0)
        group_req_layout.addWidget(self.output_dir, i, 1)
        self.input_fields["output_dir"] = self.output_dir.line_edit
        i += 1

        self.input_grid.addWidget(group_req, row, 0, 1, 2); row += 1

        # 2. Key Workflow Switches
        group_wf = QGroupBox("Key Workflow Switches")
        group_wf_layout = QGridLayout(group_wf)
        
        i = 0
        self.clean_dem = QCheckBox("Clean DEM (Requires Initial Flood Map Step)")
        self.clean_dem.setChecked(settings.value("clean_dem", False, type=bool))
        group_wf_layout.addWidget(self.clean_dem, i, 0, 1, 2); self.input_fields['clean_dem'] = self.clean_dem; i+=1

        self.estimate_consequences = QCheckBox("Estimate Consequences (Run Go-Consequences)")
        self.estimate_consequences.setChecked(settings.value("estimate_consequences", False, type=bool))
        group_wf_layout.addWidget(self.estimate_consequences, i, 0, 1, 2); self.input_fields['estimate_consequences'] = self.estimate_consequences; i+=1

        self.mapper = QComboBox()
        self.mapper.addItems(["FloodSpreader", "Curve2Flood", "FLDPLNpy"])
        self.mapper.setCurrentText(settings.value("mapper", "Curve2Flood"))
        group_wf_layout.addWidget(QLabel("Mapper Method"), i+1, 0); group_wf_layout.addWidget(self.mapper, i+1, 1); self.input_fields['mapper'] = self.mapper; i+=2

        self.streamflow_source = QComboBox()
        self.streamflow_source.addItems(list(STREAMFLOW_SOURCE_LABELS.keys()))
        self.streamflow_source.setCurrentText(settings.value("streamflow_source", "GEOGLOWS"))
        group_wf_layout.addWidget(QLabel("Streamflow Source"), i+1, 0); group_wf_layout.addWidget(self.streamflow_source, i+1, 1); self.input_fields['streamflow_source'] = self.streamflow_source; i+=2

        self.nwm_api_key = QLineEdit()
        self.nwm_api_key.setPlaceholderText("Required for NWM")
        self.nwm_api_key.setVisible(False)  # Hide by default
        self.nwm_api_key_label = QLabel("NWM API Key")
        self.nwm_api_key_label.setVisible(False)
        self.nwm_api_key.setText(settings.value("nwm_api_key", ""))
        group_wf_layout.addWidget(self.nwm_api_key_label, i+1, 0); group_wf_layout.addWidget(self.nwm_api_key, i+1, 1); self.input_fields['nwm_api_key'] = self.nwm_api_key; i+=2
        def toggle_nwm_api_key(value: str):
            is_nwm = (value == "NWM")
            self.nwm_api_key.setVisible(is_nwm)
            self.nwm_api_key_label.setVisible(is_nwm)
        self.streamflow_source.currentTextChanged.connect(toggle_nwm_api_key)

        self.input_grid.addWidget(group_wf, row, 0, 1, 2); row += 1


        # 3. Advanced / Optional Parameters
        group_adv = QGroupBox("Advanced Parameters")
        group_adv_layout = QGridLayout(group_adv)
        group_adv_layout.setColumnStretch(0, 0)  # label column = minimal
        group_adv_layout.setColumnStretch(1, 1)  # input column = expand

        
        i = 0
        # Checkboxes
        self.bathy_use_banks = QCheckBox("Bathy Use Banks")
        self.bathy_use_banks.setChecked(settings.value("bathy_use_banks", False, type=bool))
        group_adv_layout.addWidget(self.bathy_use_banks, i, 0, 1, 2); self.input_fields['bathy_use_banks'] = self.bathy_use_banks; i+=1
        
        i = 0
        # Checkboxes
        self.bathy_use_banks = QCheckBox("Bathy Use Banks")
        self.bathy_use_banks.setChecked(settings.value("bathy_use_banks", False, type=bool))
        group_adv_layout.addWidget(self.bathy_use_banks, i, 0, 1, 2); self.input_fields['bathy_use_banks'] = self.bathy_use_banks; i+=1

        self.flood_waterlc_and_strm_cells = QCheckBox("Flood LC  and Stream Cells in Flood Map")
        self.flood_waterlc_and_strm_cells.setChecked(settings.value("flood_waterlc_and_strm_cells", True, type=bool)) # Default from logic
        group_adv_layout.addWidget(self.flood_waterlc_and_strm_cells, i, 0, 1, 2); self.input_fields['flood_waterlc_and_strm_cells'] = self.flood_waterlc_and_strm_cells; i+=1
        
        self.use_specified_depth_for_bathy_mask = QCheckBox("Use Specified Depth for Bathy Mask")
        self.use_specified_depth_for_bathy_mask.setChecked(settings.value("use_specified_depth_for_bathy_mask", False, type=bool))
        group_adv_layout.addWidget(self.use_specified_depth_for_bathy_mask, i, 0, 1, 2); self.input_fields['use_specified_depth_for_bathy_mask'] = self.use_specified_depth_for_bathy_mask; i+=1

        self.find_banks_based_on_landcover = QCheckBox("Find Banks Based on Land Cover (Default=True)")
        self.find_banks_based_on_landcover.setChecked(settings.value("find_banks_based_on_landcover", True, type=bool))
        group_adv_layout.addWidget(self.find_banks_based_on_landcover, i, 0, 1, 2); self.input_fields['find_banks_based_on_landcover'] = self.find_banks_based_on_landcover; i+=1
        
        self.process_stream_network = QCheckBox("Process Stream Network")
        self.process_stream_network.setChecked(settings.value("process_stream_network", True, type=bool))
        group_adv_layout.addWidget(self.process_stream_network, i, 0, 1, 2); self.input_fields['process_stream_network'] = self.process_stream_network; i+=1
        
        self.create_reach_average_curve_file = QCheckBox("Create Reach Average Curve File")
        self.create_reach_average_curve_file.setChecked(settings.value("create_reach_average_curve_file", False, type=bool))
        group_adv_layout.addWidget(self.create_reach_average_curve_file, i, 0, 1, 2); self.input_fields['create_reach_average_curve_file'] = self.create_reach_average_curve_file; i+=1
        
        self.use_warning_flags_to_download_dem = QCheckBox("Use Warning Flags to Download DEM")
        self.use_warning_flags_to_download_dem.setChecked(settings.value("use_warning_flags_to_download_dem", False, type=bool))
        group_adv_layout.addWidget(self.use_warning_flags_to_download_dem, i, 0, 1, 2); self.input_fields['use_warning_flags_to_download_dem'] = self.use_warning_flags_to_download_dem; i+=1


        # Line Edits / Spin Boxes
        self.land_watervalue = QSpinBox(); self.land_watervalue.setRange(0, 255); 
        self.land_watervalue.setValue(settings.value("land_watervalue", 80, type=int))
        group_adv_layout.addWidget(QLabel("Land Water Value"), i+1, 0); group_adv_layout.addWidget(self.land_watervalue, i+1, 1); self.input_fields['land_watervalue'] = self.land_watervalue; i+=2

        self.age_of_forecast_days = QSpinBox(); self.age_of_forecast_days.setRange(1, 365); 
        self.age_of_forecast_days.setValue(settings.value("age_of_forecast_days", 7, type=int))
        group_adv_layout.addWidget(QLabel("Forecast Age (Days)"), i+1, 0); group_adv_layout.addWidget(self.age_of_forecast_days, i+1, 1); self.input_fields['age_of_forecast_days'] = self.age_of_forecast_days; i+=2

        self.specify_depths_for_bathy_mask = QLineEdit(settings.value("specify_depths_for_bathy_mask", "0.3, 0.6"))
        sdfbm_label = QLabel("Specific flood depths (in meters) for bathy mask")
        sdfbm_label.setWordWrap(True)
        group_adv_layout.addWidget(sdfbm_label, i+1, 0); group_adv_layout.addWidget(self.specify_depths_for_bathy_mask, i+1, 1); self.input_fields['specify_depths_for_bathy_mask'] = self.specify_depths_for_bathy_mask; i+=2

        self.geoglows_vpu = QComboBox()
        self.geoglows_vpu.addItem("")  # Optional/None
        self.geoglows_vpu.addItems(["704", "702", "703", "715", "714", "706", "713", "712", "709"])
        self.geoglows_vpu.setCurrentText(settings.value("geoglows_vpu", "704"))
        group_adv_layout.addWidget(QLabel("GEOGLOWS VPU ID"), i+1, 0); group_adv_layout.addWidget(self.geoglows_vpu, i+1, 1); self.input_fields['geoglows_vpu'] = self.geoglows_vpu; i+=2
        
        self.forensic_forecast_date = QLineEdit()
        self.forensic_forecast_date.setPlaceholderText("YYYYMMDD (Optional)")
        self.forensic_forecast_date.setText(settings.value("forensic_forecast_date", ""))
        group_adv_layout.addWidget(QLabel("Forensic Forecast Date"), i+1, 0); group_adv_layout.addWidget(self.forensic_forecast_date, i+1, 1); self.input_fields['forensic_forecast_date'] = self.forensic_forecast_date; i+=2

        # Updated to QComboBox for valid hours (0-23)
        self.forensic_forecast_hour = QComboBox()
        self.forensic_forecast_hour.addItem("") # Optional/None
        for h in range(0, 24):
             self.forensic_forecast_hour.addItem(str(h).zfill(2))
        self.forensic_forecast_hour.setCurrentText(settings.value("forensic_forecast_hour", ""))
        group_adv_layout.addWidget(QLabel("Forensic Forecast Hour"), i+1, 0); group_adv_layout.addWidget(self.forensic_forecast_hour, i+1, 1); self.input_fields['forensic_forecast_hour'] = self.forensic_forecast_hour; i+=2

        self.specified_bathyflow_field = QComboBox()
        self.specified_bathyflow_field.addItems(FLOW_FIELD_OPTIONS)
        self.specified_bathyflow_field.setEditable(True)
        self.specified_bathyflow_field.setCurrentText(settings.value("specified_bathyflow_field", "p_exceed_50"))
        group_adv_layout.addWidget(QLabel("Bathy Flow Field"), i+1, 0); group_adv_layout.addWidget(self.specified_bathyflow_field, i+1, 1); self.input_fields['specified_bathyflow_field'] = self.specified_bathyflow_field; i+=2

        self.specified_highflow_field = QComboBox()
        self.specified_highflow_field.addItems(FLOW_FIELD_OPTIONS)
        self.specified_highflow_field.setEditable(True)
        self.specified_highflow_field.setCurrentText(settings.value("specified_highflow_field", "rp100_premium"))
        group_adv_layout.addWidget(QLabel("High Flow Field"), i+1, 0); group_adv_layout.addWidget(self.specified_highflow_field, i+1, 1); self.input_fields['specified_highflow_field'] = self.specified_highflow_field; i+=2

        self.strmorder_field = QLineEdit()
        self.strmorder_field.setPlaceholderText("strmOrder (Optional)")
        self.strmorder_field.setText(settings.value("strmorder_field", ""))
        group_adv_layout.addWidget(QLabel("Stream Order Field"), i+1, 0); group_adv_layout.addWidget(self.strmorder_field, i+1, 1); self.input_fields['StrmOrder_Field'] = self.strmorder_field; i+=2

        self.downstream_link_field = QLineEdit()
        self.downstream_link_field.setPlaceholderText("DSLINKNO (Optional)")
        self.downstream_link_field.setText(settings.value("downstream_link_field", ""))
        group_adv_layout.addWidget(QLabel("Downstream Link Field"), i+1, 0); group_adv_layout.addWidget(self.downstream_link_field, i+1, 1); self.input_fields['Downstream_Link_Field'] = self.downstream_link_field; i+=2

        self.strmorder_lower = QLineEdit()
        self.strmorder_lower.setPlaceholderText("Optional integer")
        self.strmorder_lower.setText(settings.value("strmorder_lower", ""))
        group_adv_layout.addWidget(QLabel("Stream Order Lower"), i+1, 0); group_adv_layout.addWidget(self.strmorder_lower, i+1, 1); self.input_fields['StrmOrder_Lower'] = self.strmorder_lower; i+=2

        self.strmorder_upper = QLineEdit()
        self.strmorder_upper.setPlaceholderText("Optional integer")
        self.strmorder_upper.setText(settings.value("strmorder_upper", ""))
        group_adv_layout.addWidget(QLabel("Stream Order Upper"), i+1, 0); group_adv_layout.addWidget(self.strmorder_upper, i+1, 1); self.input_fields['StrmOrder_Upper'] = self.strmorder_upper; i+=2

        self.lake_filter_json = QLineEdit()
        self.lake_filter_json.setPlaceholderText("Path to JSON (Optional)")
        self.lake_filter_json.setText(settings.value("lake_filter_json", ""))
        group_adv_layout.addWidget(QLabel("Lake Filter JSON"), i+1, 0); group_adv_layout.addWidget(self.lake_filter_json, i+1, 1); self.input_fields['lake_filter_json'] = self.lake_filter_json; i+=2

        self.overwrite_forecast_floodmaps = QCheckBox("Overwrite Forecast Floodmaps")
        self.overwrite_forecast_floodmaps.setChecked(settings.value("overwrite_forecast_floodmaps", True, type=bool))
        group_adv_layout.addWidget(self.overwrite_forecast_floodmaps, i, 0, 1, 2); self.input_fields['overwrite_forecast_floodmaps'] = self.overwrite_forecast_floodmaps; i+=1

        self.remove_old_forecast_files = QCheckBox("Remove Old Forecast Files")
        self.remove_old_forecast_files.setChecked(settings.value("remove_old_forecast_files", True, type=bool))
        group_adv_layout.addWidget(self.remove_old_forecast_files, i, 0, 1, 2); self.input_fields['remove_old_forecast_files'] = self.remove_old_forecast_files; i+=1

        self.make_fist_inputs = QCheckBox("Make FIST Inputs")
        self.make_fist_inputs.setChecked(settings.value("make_fist_inputs", True, type=bool))
        group_adv_layout.addWidget(self.make_fist_inputs, i, 0, 1, 2); self.input_fields['make_fist_inputs'] = self.make_fist_inputs; i+=1

        self.dem_filter = QLineEdit()
        self.dem_filter.setPlaceholderText("a glob pattern (e.g., '*_dem.tif')")
        self.dem_filter.setText(settings.value("dem_filter", ""))
        group_adv_layout.addWidget(QLabel("DEM Filter"), i+1, 0); group_adv_layout.addWidget(self.dem_filter, i+1, 1); self.input_fields['dem_filter'] = self.dem_filter; i+=2

        self.floodmap_mode = QComboBox()
        self.floodmap_mode.addItems(["forecast", "user"])
        self.floodmap_mode.setCurrentText(settings.value("floodmap_mode", "forecast"))
        group_adv_layout.addWidget(QLabel("Floodmap Mode"), i+1, 0); group_adv_layout.addWidget(self.floodmap_mode, i+1, 1); self.input_fields['floodmap_mode'] = self.floodmap_mode; i+=2

        self.user_flow_files = QPlainTextEdit()
        self.user_flow_files.setPlainText(settings.value("user_flow_files", ""))
        group_adv_layout.addWidget(QLabel("User Flow Files (one per line)"), i+1, 0); group_adv_layout.addWidget(self.user_flow_files, i+1, 1); self.input_fields['user_flow_files'] = self.user_flow_files; i+=2

        self.make_curvefiles = QCheckBox("Make Curve Files")
        self.make_curvefiles.setChecked(settings.value("make_curvefiles", True, type=bool))
        group_adv_layout.addWidget(self.make_curvefiles, i, 0, 1, 2); self.input_fields['make_curvefiles'] = self.make_curvefiles; i+=1

        self.make_ap_database = QCheckBox("Make Area-Perimeter Database")
        self.make_ap_database.setChecked(settings.value("make_ap_database", True, type=bool))
        group_adv_layout.addWidget(self.make_ap_database, i, 0, 1, 2); self.input_fields['make_ap_database'] = self.make_ap_database; i+=1

        self.make_depth_maps = QCheckBox("Make Depth Maps")
        self.make_depth_maps.setChecked(settings.value("make_depth_maps", True, type=bool))
        group_adv_layout.addWidget(self.make_depth_maps, i, 0, 1, 2); self.input_fields['make_depth_maps'] = self.make_depth_maps; i+=1
        
        self.make_velocity_maps = QCheckBox("Make Velocity Maps")
        self.make_velocity_maps.setChecked(settings.value("make_velocity_maps", True, type=bool))
        group_adv_layout.addWidget(self.make_velocity_maps, i, 0, 1, 2); self.input_fields['make_velocity_maps'] = self.make_velocity_maps; i+=1

        self.make_wse_maps = QCheckBox("Make WSE Maps")
        self.make_wse_maps.setChecked(settings.value("make_wse_maps", True, type=bool))
        group_adv_layout.addWidget(self.make_wse_maps, i, 0, 1, 2); self.input_fields['make_wse_maps'] = self.make_wse_maps; i+=1

        self.vdt_file_extension = QComboBox()
        self.vdt_file_extension.addItems(["txt", "csv", "parquet"])
        self.vdt_file_extension.setCurrentText(settings.value("vdt_file_extension", "txt"))
        group_adv_layout.addWidget(QLabel("VDT File Extension"), i+1, 0); group_adv_layout.addWidget(self.vdt_file_extension, i+1, 1); self.input_fields['vdt_file_extension'] = self.vdt_file_extension; i+=2

        self.mannings_text_file = FilePicker(self, "", "Select Manning's n Text File", "Text Files (*.txt);;All Files (*)")
        self.mannings_text_file.line_edit.setText(settings.value("mannings_text_file", ""))
        self.input_fields['mannings_text_file'] = self.mannings_text_file.line_edit
        group_adv_layout.addWidget(QLabel("Manning's n Text File"), i+1, 0); group_adv_layout.addWidget(self.mannings_text_file, i+1, 1); i+=2

        self.bathy_args = DictTable()
        self.bathy_args.set_dict({
            "VDT_Database_NumIterations": 30,
            "Make_Output_GPKG": "True",
            "FS_ADJUST_FLOW_BY_FRACTION": 1.0,
            "TW_MultFact": 1.5, 
            "TopWidthPlausibleLimit": 2000,
            "Bathy_Trap_H": 0.2
        })
        self.bathy_args.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.bathy_args.setMinimumHeight((2 + self.bathy_args.table.rowCount()) * self.bathy_args.table.verticalHeader().defaultSectionSize())
        group_adv_layout.setRowStretch(i+1, 1)
        group_adv_layout.addWidget(QLabel("Bathymetry Arguments"), i+1, 0); group_adv_layout.addWidget(self.bathy_args, i+1, 1); self.input_fields['bathy_args'] = self.bathy_args; i+=2

        self.floodmap_args = DictTable()
        self.floodmap_args.set_dict({
            "Make_Output_GPKG": "True",
            "FS_ADJUST_FLOW_BY_FRACTION": 1.0,
            "TW_MultFact": 1.5, 
            "TopWidthPlausibleLimit": 6000
        })
        self.floodmap_args.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.floodmap_args.setMinimumHeight((2 + self.floodmap_args.table.rowCount()) * self.floodmap_args.table.verticalHeader().defaultSectionSize())
        group_adv_layout.setRowStretch(i+1, 1)
        group_adv_layout.addWidget(QLabel("Floodmap Arguments"), i+1, 0); group_adv_layout.addWidget(self.floodmap_args, i+1, 1); self.input_fields['floodmap_args'] = self.floodmap_args; i+=2
        
        self.input_grid.addWidget(group_adv, row, 0, 1, 2); row += 1


    def _get_params(self):
        """Collects all parameter values from the UI widgets."""
        params = {}
        for key, widget in self.input_fields.items():
            if isinstance(widget, QLineEdit):
                params[key] = widget.text().strip()
            elif isinstance(widget, QCheckBox):
                params[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                params[key] = widget.currentText()
                if params[key] == "": # Treat empty selection in ComboBox as None
                     params[key] = None
            elif isinstance(widget, QSpinBox):
                params[key] = widget.value()
            elif isinstance(widget, QPlainTextEdit):
                params[key] = widget.toPlainText()
            elif isinstance(widget, DictTable):
                params[key] = widget.to_dict()
        
        return params

    def start_simulation(self):
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.warning(self, "Running", "A simulation is already running.")
            return

        self.log_text.clear()
        self.log_text.append("[INFO] Collecting parameters...")
        self.results_text.setText("Running simulation...")
        self.run_button.setEnabled(False)
        self.run_button.setText("Simulation Running...")

        try:
            params = self._get_params()
            if not params['watershed_name'] or not params['flowline'] or not params['dem_dir'] or not params['output_dir']:
                raise ValueError("Watershed Name, Flowline, DEM Dir, and Output Dir are required.")
            if params.get("streamflow_source", "").upper().startswith("NWM") and not params.get("nwm_api_key"):
                raise ValueError("NWM API Key is required when Streamflow Source is NWM.")
        except Exception as e:
            self.show_error(f"Parameter Collection Error: {e}")
            self.run_button.setEnabled(True)
            self.run_button.setText("Start Simulation")
            return

        # Start the worker thread
        self.worker_thread = WorkerThread(params)
        self.worker_thread.finished_signal.connect(self.display_results)
        self.worker_thread.log_signal.connect(self.log_message)
        self.worker_thread.error_signal.connect(self.show_error)
        self.worker_thread.start()

    def log_message(self, message):
        """Appends a message to the simulation log."""
        self.log_text.append(message)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def display_results(self, watershed_name: str, timer: Timer):
        """Formats and displays the simulation time results."""
        self.log_message("[COMPLETED] Simulation finished.")
        self.run_button.setEnabled(True)
        self.run_button.setText("Start Simulation")

        if timer is None:
            self.results_text.setText(f"No timing data returned for watershed {watershed_name}.")
            return

        time_text = flood_main.simulation_times_to_strings(watershed_name, timer)
        self.results_text.setText("\n".join(time_text))

    def show_error(self, message):
        """Displays an error in the log and a pop-up with expandable technical details."""
        summary = message
        details = None

        marker = "\n\nDETAILS:\n"
        if marker in message:
            summary, details = message.split(marker, 1)

        self.log_text.append(f"[ERROR] {summary}")

        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Critical)
        dlg.setWindowTitle("Simulation Error")
        dlg.setText("An error occurred during the simulation.")
        dlg.setInformativeText(summary)

        # This enables the "Show Details..." expandable panel
        if details:
            dlg.setDetailedText(details)

        dlg.exec_()

        self.run_button.setEnabled(True)
        self.run_button.setText("Start Simulation")

    def save_settings(self):
        params = self._get_params()
        for param, value in params.items():
            if value is not None:
                settings.setValue(param, value)

def run_gui():
    """Initializes and runs the Qt GUI application."""
    # Initialize Application
    # Note: QApplication must be instantiated only once.
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    
    # Optional: Apply a dark theme for better visual separation
    # PyQt5 equivalent for QApplication.setStyle("Fusion")
    # Note: 'Fusion' style is usually available in PyQt5 installations
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
    palette.setColor(QPalette.ToolTipText, QColor(255, 255, 255))
    palette.setColor(QPalette.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.HighlightedText, QColor(0, 0, 0))
    app.setPalette(palette)

    # Run GUI
    window = FloodSimulationGUI()
    app.aboutToQuit.connect(window.save_settings)
    window.show()
    sys.exit(app.exec_()) # Use exec_() for PyQt5
