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
        self.timer.stop(self.label)

class Timer:
    def __init__(self):
        self.timers: dict[str, float] = {}
        self.times: dict[str, float] = defaultdict(float)

    def start(self, label: str):
        self.timers[label] = time.time()

    def stop(self, label: str):
        if label not in self.timers:
            raise ValueError(f"Timer for '{label}' was not started.")
        elapsed_seconds = (time.time() - self.timers[label])
        del self.timers[label]

        self.times[label] += elapsed_seconds

    def get_minutes(self, label: str) -> float:
        return round(self.times.get(label, 0.0) / 60.0, 2)
    
    def get_seconds(self, label: str) -> float:
        return round(self.times.get(label, 0.0), 2)
    
    def get_time_string(self, label: str) -> str:
        if self.times.get(label, 0.0) >= 60.0:
            remaining_seconds = self.times[label] % 60
            return f"{self.get_minutes(label)} minutes, {round(remaining_seconds, 2)} seconds"
        
        return f"{self.get_seconds(label)} seconds"
    
    def __call__(self, label: str) -> _TimerContextManager:
        return _TimerContextManager(self, label)
    

