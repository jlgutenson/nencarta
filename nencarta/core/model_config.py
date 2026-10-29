from pathlib import Path

from nencarta.core.enumerations import Mapper
from nencarta.core.inspect_config import inspect_config


class ModelConfig:
    """The ARC config, the flood mapper configs and the FIST inputs made for one workspace, and the flood mapper's
    config that burns ARC's bathymetry into the DEM (None without bathymetry)."""
    def __init__(
            self,
            arc_config: Path,
            mapper_configs: list[Path],
            fist_inputs: list[tuple],
            mapper: Mapper,
            quiet: bool = False,
            bathy_mapper_config: Path | None = None):
        self.arc_config = arc_config
        self.mapper_configs = mapper_configs
        self.fist_inputs = fist_inputs
        self.mapper = mapper
        self.quiet = quiet
        self.bathy_mapper_config = bathy_mapper_config

    @property
    def rating_curves_exist(self) -> bool:
        """Whether the rating curves the ARC config writes exist: its VDT database, or else its curve file."""
        if self.arc_config is None or not self.arc_config.exists():
            return False

        config = inspect_config(self.arc_config)
        rating_curves = config.get("Print_VDT_Database") or config.get("Print_Curve_File")
        return bool(rating_curves) and Path(rating_curves).exists()

    @property
    def burned_dem_exists(self) -> bool:
        """Whether the DEM with bathymetry the flood mapper's bathymetry config asks for exists."""
        if self.bathy_mapper_config is None or not self.bathy_mapper_config.exists():
            return False

        burned_dem = inspect_config(self.bathy_mapper_config, "FSOutBATHY")
        return bool(burned_dem) and Path(burned_dem).exists()

    def __repr__(self) -> str:
        return f"ModelConfig(arc_config={self.arc_config}, mapper={self.mapper})"
