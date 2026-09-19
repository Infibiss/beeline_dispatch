"""Validate editable scenario parameters before they can alter generated data."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import Model, Priority, Skill, TimeWindow, Transport

Minute = Annotated[int, Field(ge=0,le=1440,strict=True)]


class EngineerConfig(Model):
    count: int = Field(gt=0,strict=True)
    early_count: int = Field(ge=0,strict=True)
    early_shift: tuple[Minute,Minute]
    late_shift: tuple[Minute,Minute]
    transports: list[Transport] = Field(min_length=1)
    skill_sets: list[list[Skill]] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self):
        if self.count != len(self.transports) or self.early_count > self.count:
            raise ValueError("Count, transports and early_count do not match")
        for start,end in [self.early_shift,self.late_shift]:
            TimeWindow(start=start,end=end)
        if any(not skills or len(skills)>3 or len(skills)!=len(set(skills)) for skills in self.skill_sets):
            raise ValueError("Skill sets must contain 1–3 distinct skills")
        return self


class WorkRule(Model):
    skill: Skill
    duration_minutes: int = Field(gt=0,le=1440,strict=True)
    priority: Priority = "normal"


class EquipmentConfig(Model):
    router_probability: float = Field(ge=0,le=1)
    tv_box_probability: float = Field(ge=0,le=1)


class GeographyConfig(Model):
    radius_km: float = Field(ge=0)
    cluster_radius_km: float = Field(ge=0)


class TravelConfig(Model):
    kind: Literal["synthetic_euclidean"]
    speed_kmh: dict[Transport,Annotated[float,Field(gt=0)]]
    distance_factor: dict[Transport,Annotated[float,Field(ge=1)]]
    boarding_minutes: dict[Transport,Annotated[float,Field(ge=0)]]

    @model_validator(mode="after")
    def all_modes(self):
        if any(set(values)!=set(Transport) for values in (self.speed_kmh,self.distance_factor,self.boarding_minutes)):
            raise ValueError("Travel parameters required for all four modes")
        return self


class DemoConfig(Model):
    version: str = Field(min_length=1)
    seed: int = Field(strict=True)
    engineers: EngineerConfig
    work_rules: dict[str,WorkRule]
    equipment: EquipmentConfig
    geography: GeographyConfig
    travel: TravelConfig
    assumptions: list[str]
