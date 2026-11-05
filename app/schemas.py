from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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