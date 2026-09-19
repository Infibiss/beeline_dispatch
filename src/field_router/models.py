"""Solver-neutral contract. Time is minutes from midnight, distance is metres."""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Priority = Literal["normal", "high", "urgent"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Skill(str, Enum):
    LOCAL = "local"
    CONNECTION = "connection"
    EMERGENCY = "emergency"


class Transport(str, Enum):
    CAR = "car"
    WALK = "walk"
    BICYCLE = "bicycle"
    PUBLIC = "public"


class Equipment(Model):
    router: int = Field(default=0, ge=0, strict=True)
    tv_box: int = Field(default=0, ge=0, strict=True)


class TimeWindow(Model):
    start: int = Field(ge=0, le=1440, strict=True)
    end: int = Field(ge=0, le=1440, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("Window end precedes start")
        return self


class Point(Model):
    x_km: float
    y_km: float


class Request(Model):
    id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    address: str = Field(min_length=1)
    locality: str = ""
    date: str
    bk_type: str = ""
    hd_type: str = ""
    windows: list[TimeWindow] = Field(min_length=1)
    duration_minutes: int = Field(gt=0, le=1440, strict=True)
    skill: Skill
    required_transport: Transport | None = None
    # high (new connection) and urgent (emergency) share the same objective tier.
    priority: Priority = "normal"
    equipment: Equipment = Field(default_factory=Equipment)
    source_line: int | None = None
    provenance: dict[str, str] = Field(default_factory=dict)


class Engineer(Model):
    id: str = Field(min_length=1)
    name: str
    region: str
    start_location: str = "office"
    shift: TimeWindow
    skills: list[Skill] = Field(min_length=1, max_length=3)
    transport: Transport
    # None means morning loading from sufficient warehouse; a value is on-hand stock.
    stock: Equipment | None = None


class Scenario(Model):
    schema_version: str = "1.0"
    id: str
    region: str
    date: str
    office_address: str
    requests: list[Request]
    engineers: list[Engineer] = Field(min_length=1)
    points: dict[str, Point] = Field(default_factory=dict)
    travel_config: dict = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent(self):
        ids = [r.id for r in self.requests]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate request ID")
        if len({e.id for e in self.engineers}) != len(self.engineers):
            raise ValueError("Duplicate engineer ID")
        if any(r.region != self.region or r.date != self.date for r in self.requests):
            raise ValueError("Requests must belong to the scenario region and day")
        if any(e.region != self.region for e in self.engineers):
            raise ValueError("Engineers must belong to the scenario region")
        if set(ids) & {e.start_location for e in self.engineers}:
            raise ValueError("Request IDs must not collide with start location IDs")
        return self


class Visit(Model):
    request_id: str
    arrival: int
    start: int
    end: int
    travel_minutes: int
    distance_m: int
    explanation: str


class Route(Model):
    engineer_id: str
    visits: list[Visit]
    distance_m: int
    travel_minutes: int
    service_minutes: int
    loadout: Equipment


class Unassigned(Model):
    request_id: str
    code: str
    reason: str


class Metrics(Model):
    assigned: int
    unassigned: int
    engineers_used: int
    distance_m: int
    travel_minutes: int
    service_minutes: int


class Plan(Model):
    solver: str
    status: str
    scenario_id: str
    routes: list[Route]
    unassigned: list[Unassigned]
    metrics: Metrics
    elapsed_seconds: float = 0
    diagnostics: dict = Field(default_factory=dict)
