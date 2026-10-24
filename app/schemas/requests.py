"""Request body validation.

Pydantic models rather than hand-rolled checks so that a malformed worry tap
fails with a precise field error instead of a 500 three layers down.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

WORRY_REASONS = {
    "breathing_off",
    "not_himself",
    "color",
    "pain",
    "agitation",
    "output",
    "just_a_feeling",
}


class WorryCreate(BaseModel):
    """The one tap.

    `level` is the only required field beyond the patient -- everything else is
    optional because the entire premise is that this takes two seconds.
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: str
    level: int = Field(ge=0, le=2)
    reasons: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)
    voice_url: str | None = None
    capture_ms: int | None = Field(default=None, ge=0)

    @field_validator("reasons")
    @classmethod
    def _known_reasons(cls, v: list[str]) -> list[str]:
        unknown = set(v) - WORRY_REASONS
        if unknown:
            raise ValueError(
                f"Unknown reason(s): {sorted(unknown)}. "
                f"Allowed: {sorted(WORRY_REASONS)}"
            )
        # De-duplicate while preserving tap order.
        return list(dict.fromkeys(v))


class VitalsCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: str
    recorded_at: str | None = None
    heart_rate: int | None = Field(default=None, ge=0, le=300)
    resp_rate: int | None = Field(default=None, ge=0, le=90)
    spo2: int | None = Field(default=None, ge=0, le=100)
    systolic_bp: int | None = Field(default=None, ge=0, le=300)
    diastolic_bp: int | None = Field(default=None, ge=0, le=250)
    temperature_c: float | None = Field(default=None, ge=20.0, le=45.0)
    supplemental_o2: bool = False
    consciousness: Literal["A", "V", "P", "U"] | None = "A"


class EscalationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class OutcomeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: str
    event_type: Literal[
        "icu_transfer", "rrt_call", "code_blue", "death", "discharge"
    ]
    occurred_at: str | None = None
    note: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------


class UnitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class UserInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    full_name: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "charge_nurse", "nurse"] = "nurse"
    unit_id: str | None = None
    # Hackathon convenience: create the account with a known password instead
    # of waiting on an email round trip. Omit in anything resembling production.
    password: str | None = Field(default=None, min_length=8)


class PatientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    mrn_hash: str = Field(min_length=1, max_length=200)
    unit_id: str | None = None
    room: str | None = None
    age_band: str | None = None
    sex: str | None = None
    race_code: str | None = None
    primary_language: str | None = None


class AssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nurse_id: str
    patient_id: str
