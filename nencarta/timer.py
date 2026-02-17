from math import floor
import time
from collections import defaultdict

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

    def start(self, label: str):
        if label in self.starts:
            raise ValueError(f"Timer for '{label}' is already running.")
        self.starts[label] = time.perf_counter()

    def stop(self, label: str):
        if label not in self.starts:
            raise ValueError(f"Timer for '{label}' was not started.")
        elapsed_seconds = (time.perf_counter() - self.starts[label])
        del self.starts[label]

        self.times[label] += elapsed_seconds

    def get_minutes(self, label: str) -> float:
        return round(self.times.get(label, 0.0) / 60.0, 2)
    
    def get_seconds(self, label: str) -> float:
        return round(self.times.get(label, 0.0), 2)
    
    def get_time_string(self, label: str) -> str:
        seconds = self.get_seconds(label)
        if seconds >= 60:
            return f"{floor(self.get_minutes(label))} minutes, {round(seconds % 60, 2)} seconds"
        
        return f"{seconds} seconds"
    
    def __call__(self, label: str) -> _TimerContextManager:
        return _TimerContextManager(self, label)
    
