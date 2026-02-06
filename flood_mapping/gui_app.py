# built-in imports
import io
import sys
import os
import random
import time
import traceback

# third-party imports
import json          # <-- add this
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLineEdit, QLabel, QPushButton, QCheckBox, QComboBox,
    QTextEdit, QGroupBox, QSpinBox, QMessageBox, QScrollArea, QLabel
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QCoreApplication
from PyQt5.QtGui import QFont, QColor, QPalette, QIcon

# local imports
from . import main as flood_main

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



# --- MOCK SIMULATION CORE (Integrated for self-containment) ---

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

def _mock_geospatial_process(duration_base=5, variance=2):
    """Simulates the time taken by a complex geospatial step in minutes."""
    # Return a time in minutes
    return max(0.1, duration_base + random.uniform(-variance, variance))

def _mock_process_dem(watershed_dict, log_callback=None):
    """
    Simulates the execution flow based on parameters, calculating mock times.
    """
    if log_callback is None:
        log_callback = print

    watershed = watershed_dict['name']
    clean_dem = watershed_dict.get('clean_dem', False)
    mapper = watershed_dict.get('mapper', 'FloodSpreader')
    estimate_consequences = watershed_dict.get('estimate_consequences', False)

    # Initialize simulation times
    times = {k: 0.0 for k in SIMULATION_TIMES_MAP.keys()}
    total_sleep_time_s = 0

    def run_step(key, base_duration_m):
        nonlocal total_sleep_time_s
        # Calculate time in minutes
        duration_m = _mock_geospatial_process(base_duration_m)
        times[key] += duration_m
        
        # Convert to seconds for simulation delay
        duration_s = duration_m * 0.1 # Scale down for a quicker demo
        total_sleep_time_s += duration_s

        log_callback(f"-> {SIMULATION_TIMES_MAP[key]}: simulating for {duration_m:.2f} min (delay: {duration_s:.1f}s)")
        
        # Simulate time passing (critical for not freezing the GUI but respecting the thread)
        time.sleep(duration_s)
        QCoreApplication.processEvents() # Keep GUI responsive

    
    log_callback(f"[INFO] Starting simulated execution flow for {watershed}")

    # 1. Initial Flood Map Creation (if needed for cleaning)
    if clean_dem:
        log_callback("\n[STEP 1/6] Simulating Initial Flood Map (ARC/Mapper)...")
        run_step('arc_initial_simulation_time', 5)
        if mapper == "FloodSpreader":
            run_step('floodpsreaderpy_initial_simulation_time', 8)
        elif mapper == "Curve2Flood":
            run_step('curve2flood_initial_simulation_time', 12)
        
        # 2. DEM Cleaning
        log_callback("\n[STEP 2/6] Simulating DEM Cleaner Program...")
        run_step('dem_cleaner_simulation_time', 15)

    # 3. Bathymetry Creation
    log_callback("\n[STEP 3/6] Simulating Bathymetry (ARC/Mapper)...")
    run_step('arc_bathy_simulation_time', 10)
    if mapper == "FloodSpreader":
        run_step('floodpsreaderpy_bathy_simulation_time', 15)
    elif mapper == "Curve2Flood":
        run_step('curve2flood_bathy_simulation_time', 20)

    # 4. Forecast Flood Map
    log_callback("\n[STEP 4/6] Simulating Forecast Flood Map (Mapper)...")
    if mapper == "FloodSpreader":
        run_step('floodpsreaderpy_forecast_simulation_time', 10)
    elif mapper == "Curve2Flood":
        run_step('curve2flood_forecast_simulation_time', 18)

    # 5. FIST Input Creation (GeoJSON)
    log_callback("\n[STEP 5/6] Simulating FIST GeoJSON Creation...")
    run_step('geojson_forecast_simulation_time', 7)

    # 6. Consequences Estimation
    if estimate_consequences:
        log_callback("\n[STEP 6/6] Simulating Go-Consequences Estimation...")
        run_step('go_consequences_simulation_time', 10)

    total_time = sum(times.values())
    log_callback(f"\n[SUCCESS] Simulated total process time: {total_time:.2f} minutes.")

    return times

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

# --- WORKER THREAD ---

class WorkerThread(QThread):
    """Worker thread to run main.py via its JSON interface without blocking the GUI."""
    finished_signal = pyqtSignal(dict)
    log_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, params):
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

        def normalize_path(p):
            # For real runs, we want actual paths, not mock placeholders
            return os.path.normpath(p) if p else None

        # Parse depths from comma-separated text into floats
        if params.get("specify_depths_for_bathy_mask"):
            specify_depths = [
                float(d.strip())
                for d in params["specify_depths_for_bathy_mask"].split(",")
                if d.strip()
            ]
        else:
            specify_depths = []

        # Basic validation mirroring the GUI’s previous logic
        if params["use_specified_depth_for_bathy_mask"]:
            if params["clean_dem"] and len(specify_depths) < 2:
                raise ValueError("Clean DEM requires two depths for bathymetry mask.")
            if not params["clean_dem"] and len(specify_depths) < 1:
                raise ValueError("Non-clean DEM requires one depth for bathymetry mask.")

        watershed_dict = {
            "name": params["watershed_name"],
            "flowline": normalize_path(params["flowline"]),
            "dem_dir": normalize_path(params["dem_dir"]),
            "output_dir": normalize_path(params["output_dir"]),

            "bathy_use_banks": params["bathy_use_banks"],
            "flood_waterlc_and_strm_cells": params["flood_waterlc_and_strm_cells"],
            "land_watervalue": (
                params["land_watervalue"]
                if params["flood_waterlc_and_strm_cells"]
                else None
            ),
            "clean_dem": params["clean_dem"],
            "mapper": params["mapper"],
            "process_stream_network": params["process_stream_network"],
            "use_specified_depth_for_bathy_mask": params["use_specified_depth_for_bathy_mask"],
            "age_of_forecast_days": params["age_of_forecast_days"],
            "find_banks_based_on_landcover": params["find_banks_based_on_landcover"],
            "specify_depths_for_bathy_mask": specify_depths,
            "create_reach_average_curve_file": params["create_reach_average_curve_file"],
            "use_warning_flags_to_download_dem": params["use_warning_flags_to_download_dem"],
            "geoglows_vpu": params["geoglows_vpu"] or None,
            "forensic_forecast_date": params["forensic_forecast_date"] or None,
            "forensic_forecast_hour": params["forensic_forecast_hour"] or None,
            "specified_bathyflow_field": params["specified_bathyflow_field"],
            "specified_highflow_field": params["specified_highflow_field"],
            "StrmOrder_Field": params["StrmOrder_Field"],
            "Downstream_Link_Field": params["Downstream_Link_Field"],
            "StrmOrder_Lower": params["StrmOrder_Lower"],
            "StrmOrder_Upper": params["StrmOrder_Upper"],
            "lake_filter_json": (
                normalize_path(params["lake_filter_json"])
                if params["lake_filter_json"]
                else None
            ),
            "estimate_consequences": params["estimate_consequences"],
            "streamflow_source": params["streamflow_source"],
            "nwm_api_key": params.get("nwm_api_key"),
        }

        return watershed_dict

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
        try:
            # 1) Build watershed dict from GUI parameters
            watershed_dict = self._build_watershed_dict()

            # 2) Write JSON input file
            json_file = self._write_json_input(watershed_dict)
            self.log_signal.emit("[INFO] JSON input file created for main.py:")
            self.log_signal.emit(f"   {json_file}")

            # 3) Direct call instead of subprocess
            self.log_signal.emit("[INFO] Running main.process_json_input_serial() ...")

            # --- redirect stdout/stderr so all prints go into the GUI ---
            old_stdout, old_stderr = sys.stdout, sys.stderr
            stream = QtLogStream(self.log_signal)

            try:
                sys.stdout = stream
                sys.stderr = stream
                result = flood_main.process_json_input_serial(json_file)
            finally:
                stream.flush()
                sys.stdout = old_stdout
                sys.stderr = old_stderr

            if isinstance(result, dict):
                self.finished_signal.emit(result)
            else:
                self.finished_signal.emit({})

        except Exception as e:
            context = "Error running simulation"
            summary, details = self._format_exception(e, context=context)

            # Keep log readable (single-line-ish summary)
            self.log_signal.emit("[ERROR] " + summary.replace("\n", " | "))

            # Send both summary + full traceback to the GUI
            self.error_signal.emit(summary + "\n\nDETAILS:\n" + details)


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
        self.watershed_name = QLineEdit("Yellowstone_DEM")
        group_req_layout.addWidget(QLabel("Watershed Name"), i, 0); group_req_layout.addWidget(self.watershed_name, i, 1); self.input_fields['watershed_name'] = self.watershed_name; i+=1
        
        self.flowline = QLineEdit(os.path.join('data/flowline.gpkg'))
        group_req_layout.addWidget(QLabel("Flowline File"), i, 0); group_req_layout.addWidget(self.flowline, i, 1); self.input_fields['flowline'] = self.flowline; i+=1

        self.dem_dir = QLineEdit(os.path.join('data/dems'))
        group_req_layout.addWidget(QLabel("DEM Directory"), i, 0); group_req_layout.addWidget(self.dem_dir, i, 1); self.input_fields['dem_dir'] = self.dem_dir; i+=1
        
        self.output_dir = QLineEdit(os.path.join('output/results'))
        group_req_layout.addWidget(QLabel("Output Directory"), i, 0); group_req_layout.addWidget(self.output_dir, i, 1); self.input_fields['output_dir'] = self.output_dir; i+=1

        self.input_grid.addWidget(group_req, row, 0, 1, 2); row += 1

        # 2. Key Workflow Switches
        group_wf = QGroupBox("Key Workflow Switches")
        group_wf_layout = QGridLayout(group_wf)
        
        i = 0
        self.clean_dem = QCheckBox("Clean DEM (Requires Initial Flood Map Step)")
        group_wf_layout.addWidget(self.clean_dem, i, 0, 1, 2); self.input_fields['clean_dem'] = self.clean_dem; i+=1

        self.estimate_consequences = QCheckBox("Estimate Consequences (Run Go-Consequences)")
        group_wf_layout.addWidget(self.estimate_consequences, i, 0, 1, 2); self.input_fields['estimate_consequences'] = self.estimate_consequences; i+=1

        self.mapper = QComboBox()
        self.mapper.addItems(["Curve2Flood", "FLDPLNpy"])
        group_wf_layout.addWidget(QLabel("Mapper Method"), i+1, 0); group_wf_layout.addWidget(self.mapper, i+1, 1); self.input_fields['mapper'] = self.mapper; i+=2

        self.streamflow_source = QComboBox()
        self.streamflow_source.addItems(list(STREAMFLOW_SOURCE_LABELS.keys()))
        group_wf_layout.addWidget(QLabel("Streamflow Source"), i+1, 0); group_wf_layout.addWidget(self.streamflow_source, i+1, 1); self.input_fields['streamflow_source'] = self.streamflow_source; i+=2

        self.nwm_api_key = QLineEdit()
        self.nwm_api_key.setPlaceholderText("Required for NWM")
        group_wf_layout.addWidget(QLabel("NWM API Key"), i+1, 0); group_wf_layout.addWidget(self.nwm_api_key, i+1, 1); self.input_fields['nwm_api_key'] = self.nwm_api_key; i+=2

        self.input_grid.addWidget(group_wf, row, 0, 1, 2); row += 1


        # 3. Advanced / Optional Parameters
        group_adv = QGroupBox("Advanced Parameters")
        group_adv_layout = QGridLayout(group_adv)
        
        i = 0
        # Checkboxes
        self.bathy_use_banks = QCheckBox("Bathy Use Banks")
        group_adv_layout.addWidget(self.bathy_use_banks, i, 0, 1, 2); self.input_fields['bathy_use_banks'] = self.bathy_use_banks; i+=1

        self.flood_waterlc_and_strm_cells = QCheckBox("Flood LC  and Stream Cells in Flood Map")
        self.flood_waterlc_and_strm_cells.setChecked(True) # Default from logic
        group_adv_layout.addWidget(self.flood_waterlc_and_strm_cells, i, 0, 1, 2); self.input_fields['flood_waterlc_and_strm_cells'] = self.flood_waterlc_and_strm_cells; i+=1
        
        self.use_specified_depth_for_bathy_mask = QCheckBox("Use Specified Depth for Bathy Mask")
        group_adv_layout.addWidget(self.use_specified_depth_for_bathy_mask, i, 0, 1, 2); self.input_fields['use_specified_depth_for_bathy_mask'] = self.use_specified_depth_for_bathy_mask; i+=1

        self.find_banks_based_on_landcover = QCheckBox("Find Banks Based on Land Cover (Default=True)")
        self.find_banks_based_on_landcover.setChecked(True)
        group_adv_layout.addWidget(self.find_banks_based_on_landcover, i, 0, 1, 2); self.input_fields['find_banks_based_on_landcover'] = self.find_banks_based_on_landcover; i+=1
        
        self.process_stream_network = QCheckBox("Process Stream Network")
        self.process_stream_network.setChecked(True)
        group_adv_layout.addWidget(self.process_stream_network, i, 0, 1, 2); self.input_fields['process_stream_network'] = self.process_stream_network; i+=1
        
        self.create_reach_average_curve_file = QCheckBox("Create Reach Average Curve File")
        group_adv_layout.addWidget(self.create_reach_average_curve_file, i, 0, 1, 2); self.input_fields['create_reach_average_curve_file'] = self.create_reach_average_curve_file; i+=1
        
        self.use_warning_flags_to_download_dem = QCheckBox("Use Warning Flags to Download DEM")
        group_adv_layout.addWidget(self.use_warning_flags_to_download_dem, i, 0, 1, 2); self.input_fields['use_warning_flags_to_download_dem'] = self.use_warning_flags_to_download_dem; i+=1


        # Line Edits / Spin Boxes
        self.land_watervalue = QSpinBox(); self.land_watervalue.setRange(0, 255); self.land_watervalue.setValue(80)
        group_adv_layout.addWidget(QLabel("Land Water Value"), i+1, 0); group_adv_layout.addWidget(self.land_watervalue, i+1, 1); self.input_fields['land_watervalue'] = self.land_watervalue; i+=2

        self.age_of_forecast_days = QSpinBox(); self.age_of_forecast_days.setRange(1, 365); self.age_of_forecast_days.setValue(7)
        group_adv_layout.addWidget(QLabel("Forecast Age (Days)"), i+1, 0); group_adv_layout.addWidget(self.age_of_forecast_days, i+1, 1); self.input_fields['age_of_forecast_days'] = self.age_of_forecast_days; i+=2

        self.specify_depths_for_bathy_mask = QLineEdit("0.3, 0.6")
        group_adv_layout.addWidget(QLabel("Specific flood depths (in meters) for bathy mask"), i+1, 0); group_adv_layout.addWidget(self.specify_depths_for_bathy_mask, i+1, 1); self.input_fields['specify_depths_for_bathy_mask'] = self.specify_depths_for_bathy_mask; i+=2

        self.geoglows_vpu = QComboBox()
        self.geoglows_vpu.addItem("")  # Optional/None
        self.geoglows_vpu.addItems(["704", "702", "703", "715", "714", "706", "713", "712", "709"])
        group_adv_layout.addWidget(QLabel("GEOGLOWS VPU ID"), i+1, 0); group_adv_layout.addWidget(self.geoglows_vpu, i+1, 1); self.input_fields['geoglows_vpu'] = self.geoglows_vpu; i+=2
        
        self.forensic_forecast_date = QLineEdit()
        self.forensic_forecast_date.setPlaceholderText("YYYYMMDD (Optional)")
        group_adv_layout.addWidget(QLabel("Forensic Forecast Date"), i+1, 0); group_adv_layout.addWidget(self.forensic_forecast_date, i+1, 1); self.input_fields['forensic_forecast_date'] = self.forensic_forecast_date; i+=2

        # Updated to QComboBox for valid hours (0-23)
        self.forensic_forecast_hour = QComboBox()
        self.forensic_forecast_hour.addItem("") # Optional/None
        for h in range(0, 24):
             self.forensic_forecast_hour.addItem(str(h).zfill(2))
        group_adv_layout.addWidget(QLabel("Forensic Forecast Hour"), i+1, 0); group_adv_layout.addWidget(self.forensic_forecast_hour, i+1, 1); self.input_fields['forensic_forecast_hour'] = self.forensic_forecast_hour; i+=2

        self.specified_bathyflow_field = QComboBox()
        self.specified_bathyflow_field.addItems(FLOW_FIELD_OPTIONS)
        self.specified_bathyflow_field.setEditable(True)
        self.specified_bathyflow_field.setCurrentText("p_exceed_50")
        group_adv_layout.addWidget(QLabel("Bathy Flow Field"), i+1, 0); group_adv_layout.addWidget(self.specified_bathyflow_field, i+1, 1); self.input_fields['specified_bathyflow_field'] = self.specified_bathyflow_field; i+=2

        self.specified_highflow_field = QComboBox()
        self.specified_highflow_field.addItems(FLOW_FIELD_OPTIONS)
        self.specified_highflow_field.setEditable(True)
        self.specified_highflow_field.setCurrentText("rp100_premium")
        group_adv_layout.addWidget(QLabel("High Flow Field"), i+1, 0); group_adv_layout.addWidget(self.specified_highflow_field, i+1, 1); self.input_fields['specified_highflow_field'] = self.specified_highflow_field; i+=2

        self.strmorder_field = QLineEdit()
        self.strmorder_field.setPlaceholderText("strmOrder (Optional)")
        group_adv_layout.addWidget(QLabel("Stream Order Field"), i+1, 0); group_adv_layout.addWidget(self.strmorder_field, i+1, 1); self.input_fields['StrmOrder_Field'] = self.strmorder_field; i+=2

        self.downstream_link_field = QLineEdit()
        self.downstream_link_field.setPlaceholderText("DSLINKNO (Optional)")
        group_adv_layout.addWidget(QLabel("Downstream Link Field"), i+1, 0); group_adv_layout.addWidget(self.downstream_link_field, i+1, 1); self.input_fields['Downstream_Link_Field'] = self.downstream_link_field; i+=2

        self.strmorder_lower = QLineEdit()
        self.strmorder_lower.setPlaceholderText("Optional integer")
        group_adv_layout.addWidget(QLabel("Stream Order Lower"), i+1, 0); group_adv_layout.addWidget(self.strmorder_lower, i+1, 1); self.input_fields['StrmOrder_Lower'] = self.strmorder_lower; i+=2

        self.strmorder_upper = QLineEdit()
        self.strmorder_upper.setPlaceholderText("Optional integer")
        group_adv_layout.addWidget(QLabel("Stream Order Upper"), i+1, 0); group_adv_layout.addWidget(self.strmorder_upper, i+1, 1); self.input_fields['StrmOrder_Upper'] = self.strmorder_upper; i+=2

        self.lake_filter_json = QLineEdit()
        self.lake_filter_json.setPlaceholderText("Path to JSON (Optional)")
        group_adv_layout.addWidget(QLabel("Lake Filter JSON"), i+1, 0); group_adv_layout.addWidget(self.lake_filter_json, i+1, 1); self.input_fields['lake_filter_json'] = self.lake_filter_json; i+=2


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

        streamflow_label = params.get("streamflow_source")
        if streamflow_label:
            params["streamflow_source"] = STREAMFLOW_SOURCE_LABELS.get(streamflow_label, streamflow_label)
        
        # Convert empty strings/zero from optional fields to None/default if they should be
        if not params['forensic_forecast_date']: params['forensic_forecast_date'] = None
        if not params['geoglows_vpu']:
            params['geoglows_vpu'] = None
        else:
            params['geoglows_vpu'] = int(params['geoglows_vpu'])
        if not params['lake_filter_json']: params['lake_filter_json'] = None
        if not params.get('nwm_api_key'): params['nwm_api_key'] = None
        if not params.get('StrmOrder_Field'): params['StrmOrder_Field'] = None
        if not params.get('Downstream_Link_Field'): params['Downstream_Link_Field'] = None
        for key in ('StrmOrder_Lower', 'StrmOrder_Upper'):
            if not params.get(key):
                params[key] = None
            else:
                params[key] = int(params[key])
        
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

    def display_results(self, times):
        """Formats and displays the simulation time results."""
        self.log_message("[COMPLETED] Simulation finished.")
        self.run_button.setEnabled(True)
        self.run_button.setText("Start Simulation")

        if not times:
            self.results_text.setText("No timing data returned.")
            return

        total_time = sum(times.values())
        
        html_output = f"<h3 style='color: #4CAF50;'>Total Simulated Process Time: {total_time:.2f} minutes</h3>"
        html_output += "<table width='100%' border='0' cellspacing='5' cellpadding='5'>"
        
        # Sort keys to display in a logical order
        sorted_keys = sorted(SIMULATION_TIMES_MAP.keys(), key=lambda k: SIMULATION_TIMES_MAP[k])
        
        for key in sorted_keys:
            name = SIMULATION_TIMES_MAP.get(key, key)
            value = times.get(key, 0.0)
            
            # Highlight non-zero times
            color = "#444"
            if value > 0:
                color = "#007BFF" # Blue for active steps
                if 'Forecast' in name:
                    color = "#FFC107" # Yellow for forecast steps
                elif 'Bathy' in name:
                    color = "#17A2B8" # Teal for bathy steps
            
            html_output += f"<tr>"
            html_output += f"<td width='70%' style='color: {color};'><b>{name}:</b></td>"
            html_output += f"<td width='30%' align='right' style='color: {color};'>{value:.2f} min</td>"
            html_output += f"</tr>"

        html_output += "</table>"
        self.results_text.setText(html_output)

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
    window.show()
    sys.exit(app.exec_()) # Use exec_() for PyQt5
