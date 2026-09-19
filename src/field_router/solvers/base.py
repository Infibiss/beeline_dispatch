from typing import Protocol

from ..models import Plan, Scenario
from ..travel import TravelProvider


class Solver(Protocol):
    def solve(self, scenario: Scenario, travel: TravelProvider, *, time_limit_seconds: float = 5) -> Plan: ...
