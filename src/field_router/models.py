"""Solver-neutral contract. Time is minutes from midnight, distance is metres."""
from datetime import date
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


class VehicleFailure(Model):
    """A factual fleet incident. It is evidence, not a calibrated probability."""

    occurred_on: str
    kind: str = Field(min_length=1, max_length=120)
    severity: Literal["minor", "major", "critical"] = "minor"
    odometer_km: int | None = Field(default=None, ge=0, strict=True)
    downtime_hours: float = Field(default=0, ge=0, le=24 * 365)
    resolved: bool = True

    @model_validator(mode="after")
    def valid_date(self):
        date.fromisoformat(self.occurred_on)
        return self


class VehicleMaintenance(Model):
    """A completed maintenance action used to audit the fleet data lineage."""

    performed_on: str
    odometer_km: int = Field(ge=0, strict=True)
    kind: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_date(self):
        date.fromisoformat(self.performed_on)
        return self


class Vehicle(Model):
    """Optional physical vehicle data used by the transparent fleet risk score."""

    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["car", "van", "special"] = "car"
    odometer_km: int | None = Field(default=None, ge=0, strict=True)
    odometer_observed_on: str | None = None
    next_service_km: int | None = Field(default=None, ge=0, strict=True)
    next_service_date: str | None = None
    open_defects: int = Field(default=0, ge=0, le=100, strict=True)
    maintenance: list[VehicleMaintenance] = Field(default_factory=list, max_length=500)
    failures: list[VehicleFailure] = Field(default_factory=list, max_length=500)
    active: bool = True
    source: Literal["provided", "synthetic_demo", "unknown"] = "provided"

    @model_validator(mode="after")
    def valid_service_date(self):
        for value in (self.odometer_observed_on, self.next_service_date):
            if value is not None:
                date.fromisoformat(value)
        if self.odometer_km is not None and any(item.odometer_km > self.odometer_km for item in self.maintenance):
            raise ValueError("Maintenance odometer exceeds the current odometer")
        return self


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
    # Emergency > connection > repair/additional order.
    priority: Priority = "normal"
    equipment: Equipment = Field(default_factory=Equipment)
    source_line: int | None = None
    provenance: dict[str, str] = Field(default_factory=dict)


class Engineer(Model):
    id: str = Field(min_length=1)
    name: str
    region: str
    start_location: str = "office"
    start_address: str | None = None
    shift: TimeWindow
    skills: list[Skill] = Field(min_length=1, max_length=3)
    transport: Transport
    vehicle_id: str | None = None
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
    vehicles: list[Vehicle] = Field(default_factory=list)
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
        vehicle_ids = [v.id for v in self.vehicles]
        if len(vehicle_ids) != len(set(vehicle_ids)):
            raise ValueError("Duplicate vehicle ID")
        linked = [e.vehicle_id for e in self.engineers if e.vehicle_id is not None]
        if len(linked) != len(set(linked)):
            raise ValueError("One vehicle cannot be assigned to multiple engineers")
        if set(linked) - set(vehicle_ids):
            raise ValueError("Engineer references an unknown vehicle")
        if any(e.vehicle_id is not None and e.transport != Transport.CAR for e in self.engineers):
            raise ValueError("Only an engineer travelling by car can have a vehicle")
        scenario_day = date.fromisoformat(self.date)
        if any(date.fromisoformat(item.occurred_on) > scenario_day for v in self.vehicles for item in v.failures):
            raise ValueError("Vehicle failure history cannot be in the future")
        if any(date.fromisoformat(item.performed_on) > scenario_day for v in self.vehicles for item in v.maintenance):
            raise ValueError("Vehicle maintenance history cannot be in the future")
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
