"""Geography is separate from synthetic Cartesian points and versioned by input."""
from typing import Literal
from pydantic import Field, model_validator
import math
from .models import Model


class GeoLocation(Model):
    lat: float = Field(ge=-90,le=90)
    lon: float = Field(ge=-180,le=180)
    quality: Literal['building_match','manual','approximate']
    source: str = Field(min_length=1)
    label: str = ''
    query: str = ''
    osm_id: str = ''
    original_address: str = ''
    approximation_reason: str = ''

    @model_validator(mode='after')
    def approximate_provenance(self):
        if self.quality=='approximate' and not (self.original_address.strip() and self.label.strip() and self.approximation_reason.strip()):
            raise ValueError('Приближение требует исходного адреса, выбранного места и причины')
        return self


class GeographyBundle(Model):
    schema_version: str = '1.0'
    scenario_signature: str
    locations: dict[str,GeoLocation]
    unresolved: dict[str,dict] = Field(default_factory=dict)
    modes: dict[str,dict] = Field(default_factory=dict)
    matrices: dict = Field(default_factory=dict)
    route_shapes: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


    @model_validator(mode="after")
    def valid_shapes(self):
        for routes in self.route_shapes.values():
            if not isinstance(routes,dict): raise ValueError("Неверная структура геометрии маршрутов")
            for shape in routes.values():
                if not isinstance(shape,dict) or shape.get('kind') not in ('road','estimate'):
                    raise ValueError("Не указан тип геометрии")
                coordinates=shape.get('coordinates')
                if not isinstance(coordinates,list) or len(coordinates)<2:
                    raise ValueError("Геометрия должна содержать список координат")
                for point in coordinates:
                    if not isinstance(point,(tuple,list)) or len(point)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in point):
                        raise ValueError("Неверная пара координат геометрии")
                    if not (-180<=point[0]<=180 and -90<=point[1]<=90):
                        raise ValueError("Координаты геометрии за пределами WGS84")
        return self
