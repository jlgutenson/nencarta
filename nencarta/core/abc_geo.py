from abc import ABC, abstractmethod


class GISDataSource(ABC):
    @classmethod
    def bounds_intersect(cls, bounds1: tuple[float, float, float, float], bounds2: tuple[float, float, float, float]) -> bool:
        minx1, miny1, maxx1, maxy1 = bounds1
        minx2, miny2, maxx2, maxy2 = bounds2

        return not (maxx1 <= minx2 or maxx2 <= minx1 or maxy1 <= miny2 or maxy2 <= miny1)
    
    @property
    @abstractmethod
    def projection(self) -> str:
        pass

    @property
    @abstractmethod
    def bbox(self) -> str:
        pass

    @property
    @abstractmethod
    def epsg_4326_bbox(self) -> str:
        pass