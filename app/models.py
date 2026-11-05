import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

utcnow = lambda: datetime.now(timezone.utc)


def genarateID(cls):
    return Enum(cls, native_enum=False, length=20, validate_strings=True)

class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FileKind(StrEnum):
    INPUT = "input"
    INTERMEDIATE = "intermediate"
    OUTPUT = "output"


class FileReference(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20))
    storage_path: Mapped[str] = mapped_column(String(512), unique=True)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_files_expires_at", "expires_at"),)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    api_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING)
    pipeline: Mapped[list] = mapped_column(JSON)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0)

    input_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id"))
    output_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    error_message: Mapped[str | None] = mapped_column(Text)

    worker_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    input_file: Mapped[FileReference] = relationship(foreign_keys=[input_file_id])
    output_file: Mapped[FileReference | None] = relationship(foreign_keys=[output_file_id])
    steps: Mapped[list["JobStep"]] = relationship(
        back_populates="job",
        order_by="JobStep.step_index",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)


class JobStep(Base):
    __tablename__ = "job_steps"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    step_index: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=StepStatus.PENDING)

    input_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    output_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    error_message: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    progress_percent: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="steps")
    input_file: Mapped[FileReference | None] = relationship(foreign_keys=[input_file_id])
    output_file: Mapped[FileReference | None] = relationship(foreign_keys=[output_file_id])

    __table_args__ = (UniqueConstraint("job_id", "step_index", name="uq_job_step_index"),)

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds() * 1000)
        return None