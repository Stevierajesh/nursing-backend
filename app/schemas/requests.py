"""Request body validation.

Pydantic models rather than hand-rolled checks so that a malformed worry tap
fails with a precise field error instead of a 500 three layers down.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.simulation import SimulationConfig

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
    level: int = Field(strict=True, ge=0, le=4)
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
    glucose_mg_dl: int | None = Field(default=None, ge=1, le=2000)
    heart_rate: int | None = Field(default=None, ge=0, le=300)
    resp_rate: int | None = Field(default=None, ge=0, le=90)
    spo2: int | None = Field(default=None, ge=0, le=100)
    systolic_bp: int | None = Field(default=None, ge=0, le=300)
    diastolic_bp: int | None = Field(default=None, ge=0, le=250)
    temperature_c: float | None = Field(default=None, ge=20.0, le=45.0)
    # Numeric rating scale. Not a NEWS2 parameter: news_score ignores it, and
    # it is banded separately in services/bands.py.
    pain_score: int | None = Field(default=None, ge=0, le=10)
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
    role: Literal["admin", "charge_nurse", "nurse", "prescriber"] = "nurse"
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
    bed: str | None = Field(default=None, max_length=40)
    age_band: str | None = None
    sex: str | None = None
    race_code: str | None = None
    primary_language: str | None = None
    simulation: SimulationConfig | None = None
    nurse_id: str | None = None


class AssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nurse_id: str
    patient_id: str


class BedCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    unit_id: str
    room: str = Field(min_length=1, max_length=40)
    bed: str = Field(min_length=1, max_length=40)


class LocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bed_id: str | None


class NurseNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    body: str = Field(min_length=1, max_length=4000)


class MedicationReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    patient_id: str
    prescriber_id: str
    condition: Literal["blood_pressure", "blood_sugar", "heart_rate"]
    request_note: str = Field(min_length=1, max_length=2000)
    # The nurse's suggested class, chosen from /medication/classes. Optional:
    # a free-text request with no class is still a valid thing to send.
    suggested_class_id: str | None = None


class PrescriptionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    medication: str = Field(min_length=1, max_length=200)
    dose: str = Field(min_length=1, max_length=200)
    route: str = Field(min_length=1, max_length=100)
    frequency: str = Field(min_length=1, max_length=200)
    duration: str = Field(min_length=1, max_length=200)
    instructions: str = Field(default="", max_length=2000)


class MedicationReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # approve/reject are the prescriber's clinical decision on the class the
    # nurse suggested. acknowledge ("I have seen this") and request_info ("I
    # need something before I can answer") both leave the request open and
    # neither pauses the escalation clock.
    # "review" is the legacy two-state action, kept so existing clients and the
    # dashboard keep working; new callers should send approve or reject.
    action: Literal["approve", "reject", "acknowledge", "request_info", "review", "dismiss"]
    note: str = Field(min_length=1, max_length=2000)
    prescriptions: list[PrescriptionDraft] | None = Field(default=None, min_length=1, max_length=20)
    reviewed_sections: list[int] | None = None

    @model_validator(mode="after")
    def validate_prescription_decision(self):
        if self.prescriptions is not None:
            if self.action != "approve":
                raise ValueError("Prescription details can only be saved with approval")
            if self.reviewed_sections is None or sorted(self.reviewed_sections) != list(range(8)):
                raise ValueError("Review all eight sections before approving prescriptions")
        elif self.reviewed_sections is not None:
            raise ValueError("Section confirmations require prescription details")
        return self


class PatientAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str = Field(min_length=1, max_length=200)
    unit_id: UUID
    bed_id: UUID | None = None
