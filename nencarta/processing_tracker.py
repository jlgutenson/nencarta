from __future__ import annotations

from collections import defaultdict
from math import floor
import ctypes
import os
import sys
import threading
import time


if os.name == "nt":
    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
    _KERNEL32.GetCurrentProcess.restype = ctypes.c_void_p
    _PSAPI.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        ctypes.c_ulong,
    ]
    _PSAPI.GetProcessMemoryInfo.restype = ctypes.c_int
elif sys.platform == "darwin":
    class MACH_TASK_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("resident_size_max", ctypes.c_uint64),
            ("user_time", ctypes.c_uint64),
            ("system_time", ctypes.c_uint64),
            ("policy", ctypes.c_int),
            ("suspend_count", ctypes.c_int),
        ]

    _LIBC = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
    _MACH_TASK_BASIC_INFO = 20
    _MACH_TASK_BASIC_INFO_COUNT = ctypes.c_uint32(
        ctypes.sizeof(MACH_TASK_BASIC_INFO) // ctypes.sizeof(ctypes.c_uint32)
    )
    _LIBC.mach_task_self.restype = ctypes.c_uint32
    _LIBC.task_info.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    _LIBC.task_info.restype = ctypes.c_int


def _get_process_memory_bytes() -> int | None:
    if os.name == "nt":
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        process_handle = _KERNEL32.GetCurrentProcess()
        success = _PSAPI.GetProcessMemoryInfo(
            process_handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if success:
            return int(counters.WorkingSetSize)
        return None
    if sys.platform == "darwin":
        info = MACH_TASK_BASIC_INFO()
        info_count = ctypes.c_uint32(_MACH_TASK_BASIC_INFO_COUNT.value)
        result = _LIBC.task_info(
            _LIBC.mach_task_self(),
            _MACH_TASK_BASIC_INFO,
            ctypes.byref(info),
            ctypes.byref(info_count),
        )
        if result == 0:
            return int(info.resident_size)
        return None

    proc_status = "/proc/self/status"
    if os.path.exists(proc_status):
        with open(proc_status, "r", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024

    return None


class _PeakMemorySampler:
    def __init__(self, sample_interval_seconds: float = 0.1):
        self.sample_interval_seconds = sample_interval_seconds
        self._stop_event = threading.Event()
        self._peak_bytes = _get_process_memory_bytes()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._peak_bytes is None:
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> int | None:
        if self._peak_bytes is None:
            return None

        self._sample_once()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.sample_interval_seconds * 2)
        self._sample_once()
        return self._peak_bytes

    def _run(self):
        while not self._stop_event.wait(self.sample_interval_seconds):
            self._sample_once()

    def _sample_once(self):
        current_bytes = _get_process_memory_bytes()
        if current_bytes is not None:
            self._peak_bytes = max(self._peak_bytes or 0, current_bytes)

class _TimerContextManager:
    def __init__(self, timer: 'Timer', label: str):
        self.timer = timer
        self.label = label
        
    def __enter__(self):
        self.timer.start(self.label)
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        if self.label in self.timer.starts:
            self.timer.stop(self.label)

class Timer:
    def __init__(self):
        self.starts: dict[str, float] = {}
        self.times: dict[str, float] = defaultdict(float)
        self._memory_tracking_available = _get_process_memory_bytes() is not None
        self.peak_memory_bytes: dict[str, int] = defaultdict(int)
        self._memory_samplers: dict[str, _PeakMemorySampler] = {}

    def start(self, label: str):
        if label in self.starts:
            raise ValueError(f"Timer for '{label}' is already running.")
        self.starts[label] = time.perf_counter()
        if self._memory_tracking_available:
            sampler = _PeakMemorySampler()
            sampler.start()
            self._memory_samplers[label] = sampler

    def stop(self, label: str):
        if label not in self.starts:
            raise ValueError(f"Timer for '{label}' was not started.")
        elapsed_seconds = (time.perf_counter() - self.starts[label])
        del self.starts[label]

        self.times[label] += elapsed_seconds
        if self._memory_tracking_available:
            sampler = self._memory_samplers.pop(label, None)
            if sampler is not None:
                peak_bytes = sampler.stop()
                if peak_bytes is not None:
                    self.peak_memory_bytes[label] = max(self.peak_memory_bytes[label], peak_bytes)

    def get_minutes(self, label: str) -> float:
        return round(self.times.get(label, 0.0) / 60.0, 2)
    
    def get_seconds(self, label: str) -> float:
        return round(self.times.get(label, 0.0), 2)
    
    def get_time_string(self, label: str) -> str:
        seconds = self.get_seconds(label)
        if seconds >= 60:
            return f"{floor(self.get_minutes(label))} minutes, {round(seconds % 60, 2)} seconds"
        
        return f"{seconds} seconds"

    def get_peak_memory_bytes(self, label: str) -> int | None:
        if not self._memory_tracking_available:
            return None
        return self.peak_memory_bytes.get(label, 0)

    def get_peak_memory_string(self, label: str) -> str:
        peak_bytes = self.get_peak_memory_bytes(label)
        if peak_bytes is None:
            return "unavailable"

        peak_megabytes = peak_bytes / (1024 ** 2)
        if peak_megabytes >= 1024:
            return f"{round(peak_megabytes / 1024, 2)} GB"
        return f"{round(peak_megabytes, 2)} MB"
    
    def __call__(self, label: str) -> _TimerContextManager:
        return _TimerContextManager(self, label)

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_memory_samplers"] = {}
        return state
