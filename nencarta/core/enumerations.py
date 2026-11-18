try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

class NencartaEnum(StrEnum):
    """A string enum read from a config value. from_string ignores case."""
    @classmethod
    def from_string(cls, value: str) -> Self:
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(
                f"Invalid value '{value}'. "
                f"Valid values: {[m.value for m in cls]}"
            )
        
    @classmethod
    def from_string_exact_case(cls, value: str) -> Self:
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"Invalid value '{value}'. "
                f"Valid values: {[m.value for m in cls]}"
            )

class FloodMapMode(NencartaEnum):
    FORECAST = 'forecast'
    USER = 'user'
    RETURN_PERIOD = 'return_period'

class StreamflowSource(NencartaEnum):
    """
    Where the flows come from. 'geoglows' (from before GEOGLOWS V3) means GEOGLOWS V2. CUSTOM downloads nothing: the
    flows come from your files, and the stream network's ID fields have to be given.
    """
    GEOGLOWS_V2 = 'geoglows_v2'
    GEOGLOWS = 'geoglows_v2'  # the same source, by its old name
    NWM_SHORT_RANGE = 'nwm_short_range'
    NWM_MEDIUM_RANGE = 'nwm_medium_range'
    NWM_LONG_RANGE = 'nwm_long_range'
    CUSTOM = 'custom'

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str) and value.lower() == 'geoglows':
            return cls.GEOGLOWS_V2
        return None

    def is_nwm(self) -> bool:
        return self in {
            self.NWM_SHORT_RANGE,
            self.NWM_MEDIUM_RANGE,
            self.NWM_LONG_RANGE,
        }

    @property
    def file_prefix(self) -> str:
        """What output file names start with: GEOGLOWS V2's are 'GEOGLOWS_', as they were before V3."""
        if self == self.GEOGLOWS_V2:
            return 'GEOGLOWS'
        return str(self)

    def __str__(self):
        if self.is_nwm():
            return self.value[:3].upper() + self.value[3:]
        return self.value.upper()

class Mapper(NencartaEnum):
    """The flood mappers. Unlike the other enums, their names are case sensitive; None or '' is Kernel Weighted."""
    FLOODSPREADER = 'FloodSpreader'
    CURVE2FLOOD_KERNEL_WEIGHTED = 'Curve2Flood-Kernel Weighted'
    CURVE2FLOOD_FLDPLNPY = 'Curve2Flood-FLDPLNpy'
    CURVE2FLOOD_MULTI_POINT_INTERPOLATION = 'Curve2Flood-Multi-Point Interpolation'

    @classmethod
    def from_string(cls, value):
        return cls.from_string_exact_case(value)
    
    @classmethod
    def from_string_exact_case(cls, value):
        if value is None:
            return Mapper.CURVE2FLOOD_KERNEL_WEIGHTED

        value = str(value).strip()
        if value == "":
            return Mapper.CURVE2FLOOD_KERNEL_WEIGHTED
        
        return super().from_string_exact_case(value)
    
    @classmethod
    def list_names(cls):
        return [m.value for m in cls]
    
    def is_curve2flood_fldpln_mapper(self) -> bool:
        return self == self.CURVE2FLOOD_FLDPLNPY

    def is_curve2flood_mapper(self) -> bool:
        return self != self.FLOODSPREADER
