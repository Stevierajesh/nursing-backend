"""Configuration for authored demo trajectories, independent of nurse worry."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BaselineVitals(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    heart_rate: int = Field(default=78, strict=True, ge=40, le=140)
    resp_rate: int = Field(default=16, strict=True, ge=8, le=30)
    spo2: int = Field(default=98, strict=True, ge=85, le=100)
    systolic_bp: int = Field(default=124, strict=True, ge=85, le=200)
    diastolic_bp: int = Field(default=78, strict=True, ge=45, le=120)
    temperature_c: float = Field(default=36.8, ge=35, le=39.5)

    @model_validator(mode="after")
    def pressure_order(self):
        if self.diastolic_bp >= self.systolic_bp:
            raise ValueError("Diastolic pressure must be lower than systolic pressure")
        return self


class SimulationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    scenario: Literal["stable", "respiratory_decline", "circulatory_decline", "recovery"] = "stable"
    health_context: str = Field(default="", max_length=1000)
    baseline: BaselineVitals = Field(default_factory=BaselineVitals)
    duration_minutes: int = Field(default=10, strict=True, ge=2, le=240)
    onset_minutes: int = Field(default=1, strict=True, ge=0, le=120)
    seed: int = Field(default=42, strict=True, ge=0, le=2147483647)

    @model_validator(mode="after")
    def onset_before_end(self):
        if self.onset_minutes >= self.duration_minutes:
            raise ValueError("Deterioration onset must precede the end of the run")
        return self


class SimulationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "pause", "resume", "restart", "stop"]

