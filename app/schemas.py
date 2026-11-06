from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.steps import STEP_PARAMS


class StepIn(BaseModel):
    step: str
    params: dict = {}

    @model_validator(mode="after")
    def check_step_and_params(self):
        if self.step not in STEP_PARAMS:
            raise ValueError(f"unknown step '{self.step}', expected one of {sorted(STEP_PARAMS)}")
        unknown = set(self.params) - STEP_PARAMS[self.step]
        if unknown:
            raise ValueError(
                f"step '{self.step}' got unknown params {sorted(unknown)}, "
                f"allowed: {sorted(STEP_PARAMS[self.step])}"
            )
        return self


class JobCreated(BaseModel):
    id: UUID
    status: str


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step_index: int
    step_type: str
    status: str
    progress_percent: int | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    current_step_index: int
    output_file_id: UUID | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    steps: list[StepOut]