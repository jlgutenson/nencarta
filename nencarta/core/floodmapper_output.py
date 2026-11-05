from dataclasses import dataclass
from pathlib import Path

import yaml


def _path_or_none(path_str):
    if path_str is None:
        return None
    return Path(path_str)

@dataclass
class FloodMapperOutput:
    """The maps one flood mapper config writes."""
    config_path: Path = None
    floodmap: Path = None
    floodmap_vector: Path = None
    velocity_map: Path = None
    depth_map: Path = None
    wse_map: Path = None

    @classmethod
    def from_config(cls, config_path: Path):
        with open(config_path, 'r') as f:
            if config_path.suffix in ['.yaml', '.yml']:
                config = yaml.safe_load(f)
            else:
                config = {}
                for line in f:
                    if not line.strip() or line.strip().startswith('#'):
                        continue
  
                    key, value = line.split(maxsplit=1)
                    config[key.strip()] = value.strip()

        return cls(
            config_path=config_path,
            floodmap=_path_or_none(config.get('OutFLD')),
            floodmap_vector=_path_or_none(config.get('OutSHP')),
            velocity_map=_path_or_none(config.get('OutVEL')),
            depth_map=_path_or_none(config.get('OutDEP')),
            wse_map=_path_or_none(config.get('OutWSE')),
        )
    
    def existing_rasters(self) -> list[Path]:
        existing_rasters = []
        if self.floodmap and self.floodmap.exists():
            existing_rasters.append(self.floodmap)
        if self.velocity_map and self.velocity_map.exists():
            existing_rasters.append(self.velocity_map)
        if self.depth_map and self.depth_map.exists():
            existing_rasters.append(self.depth_map)
        if self.wse_map and self.wse_map.exists():
            existing_rasters.append(self.wse_map)
        
        return existing_rasters

class FloodMapperBulkOutput:
    """The maps of several flood mapper configs."""
    def __init__(self, config_paths: list[Path]):
        self.config_paths = config_paths
        self.outputs = [FloodMapperOutput.from_config(p) for p in config_paths]

    def existing_rasters(self) -> list[Path]:
        existing_rasters = []
        for output in self.outputs:
            existing_rasters.extend(output.existing_rasters())
        return existing_rasters
    