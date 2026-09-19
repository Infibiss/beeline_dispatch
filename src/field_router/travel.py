"""Travel matrices are independent of solvers and report their origin."""
import math
from typing import Protocol

from .models import Scenario, Transport


class TravelProvider(Protocol):
    def minutes(self, origin: str, destination: str, mode: Transport) -> int: ...
    def metres(self, origin: str, destination: str, mode: Transport) -> int: ...


class MatrixTravel:
    def __init__(self, locations: list[str], minutes: dict, metres: dict):
        if len(set(locations)) != len(locations):
            raise ValueError("Duplicate travel location")
        self.locations = list(locations)
        self.index = {key:i for i,key in enumerate(locations)}
        self._minutes, self._metres = {}, {}
        n = len(locations)
        for mode in Transport:
            for source, target in [(minutes,self._minutes),(metres,self._metres)]:
                matrix = source[mode]
                if len(matrix) != n or any(len(row)!=n for row in matrix):
                    raise ValueError("Invalid matrix dimensions")
                if any(type(x) is not int or x < 0 for row in matrix for x in row):
                    raise ValueError("Matrix values must be nonnegative integers")
                if any(matrix[i][i] != 0 for i in range(n)):
                    raise ValueError("Matrix diagonal must be zero")
                target[mode] = tuple(tuple(row) for row in matrix)

    def minutes(self, origin, destination, mode):
        return self._minutes[mode][self.index[origin]][self.index[destination]]

    def metres(self, origin, destination, mode):
        return self._metres[mode][self.index[origin]][self.index[destination]]


def synthetic_travel(scenario: Scenario) -> MatrixTravel:
    if scenario.metadata.get("geography") != "synthetic_cartesian_km":
        raise ValueError("Synthetic provider requires explicit synthetic geography")
    cfg = scenario.travel_config
    ids = list(scenario.points)
    expected = {r.id for r in scenario.requests} | {e.start_location for e in scenario.engineers}
    if not expected.issubset(ids):
        raise ValueError("Missing coordinates")
    times, distances = {}, {}
    for mode in Transport:
        speed, factor = cfg["speed_kmh"][mode.value], cfg["distance_factor"][mode.value]
        overhead = cfg["boarding_minutes"][mode.value]
        if not all(math.isfinite(x) for x in (speed,factor,overhead)) or speed <= 0 or factor < 1 or overhead < 0:
            raise ValueError("Invalid synthetic travel parameters")
        distances[mode], times[mode] = [], []
        for a in ids:
            dm, tm = [], []
            for b in ids:
                pa, pb = scenario.points[a], scenario.points[b]
                metres = round(math.hypot(pa.x_km-pb.x_km,pa.y_km-pb.y_km)*1000*factor)
                dm.append(metres)
                tm.append(0 if metres == 0 else math.ceil(metres/1000/speed*60+overhead))
            distances[mode].append(dm)
            times[mode].append(tm)
    return MatrixTravel(ids,times,distances)
