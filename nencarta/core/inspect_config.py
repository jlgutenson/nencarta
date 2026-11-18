from pathlib import Path
from typing import Any

import yaml


def inspect_config(config_path: Path, key: str | None = None) -> dict[str, Any] | Any:
    """Read an ARC or Curve2Flood config (text or YAML) as a dictionary, or just the value of ``key``."""
    with open(config_path, 'r') as f:
        if config_path.suffix in ['.yaml', '.yml']:
            config: dict[str, Any] = yaml.safe_load(f)
        else:
            config = {}
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                k, value = line.split(maxsplit=1)
                config[k] = value.strip()

    if key is not None:
        return config.get(key)

    return config