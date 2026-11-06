import os
import signal
import socket
import threading
import time
import uuid
from datetime import timedelta

import structlog
from sqlalchemy import and_, or_, select, update

from app.config import settings
from app.db import SessionLocal, ping
from app.logging_setup import setup_logging
from app.models import FileKind, FileReference, Job, JobStatus, JobStep, StepStatus, utcnow
from app.steps import STEPS, StepError
from app.storage import full_path

setup_logging()
log = structlog.get_logger()

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
running = True


def stop(*_):
    global running
    running = False
    log.info("worker_stopping", worker_id=WORKER_ID)


def claim_next_job() -> uuid.UUID | None:
    stale_before = utcnow() - timedelta(seconds=settings.stale_job_seconds)
    with SessionLocal() as session:
        job = session.scalars(
            select(Job)
            .where(or_(
                Job.status == JobStatus.PENDING,
                and_(Job.status == JobStatus.PROCESSING, Job.heartbeat_at < stale_before),
            ))
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()

        if job is None:
            return None

        if job.status == JobStatus.PROCESSING:
            log.warning("job_reclaimed", job_id=str(job.id), previous_worker=job.worker_id)

        job.status = JobStatus.PROCESSING
        job.worker_id = WORKER_ID
        job.started_at = job.started_at or utcnow()
        job.heartbeat_at = utcnow()
        session.commit()
        return job.id


class Heartbeat(threading.Thread):
    """Touches job.heartbeat_at every few seconds while the job is being processed."""

    def __init__(self, job_id: uuid.UUID):
        super().__init__(daemon=True)
        self.job_id = job_id
        self.done = threading.Event()

    def run(self):
        while not self.done.wait(settings.heartbeat_seconds):
            try:
                with SessionLocal() as session:
                    if self.step_is_stuck(session):
                        self.give_up(session)
                    session.execute(
                        update(Job)
                        .where(Job.id == self.job_id, Job.worker_id == WORKER_ID)
                        .values(heartbeat_at=utcnow())
                    )
                    session.commit()
            except Exception:
                log.exception("heartbeat_failed", job_id=str(self.job_id))

    def step_is_stuck(self, session) -> bool:
        too_old = utcnow() - timedelta(seconds=settings.step_timeout_seconds)
        stuck = session.scalar(
            select(JobStep.id).where(
                JobStep.job_id == self.job_id,
                JobStep.status == StepStatus.RUNNING,
                JobStep.started_at < too_old,
            )
        )
        return stuck is not None

    def give_up(self, session):
        log.error("step_timed_out", job_id=str(self.job_id), worker_id=WORKER_ID,
                  timeout_seconds=settings.step_timeout_seconds)
        # Mark the job as abandoned right away, so another worker picks it up now.
        expired = utcnow() - timedelta(seconds=settings.stale_job_seconds + 1)
        session.execute(
            update(Job)
            .where(Job.id == self.job_id, Job.worker_id == WORKER_ID)
            .values(heartbeat_at=expired)
        )
        session.commit()
        # The main thread is stuck and can't be interrupted, so exit the whole
        # process. Docker's restart policy brings the worker back.
        os._exit(1)


def run_step(session, step, src, dst):
    run = STEPS.get(step.step_type)
    if run is None:
        raise StepError(f"step '{step.step_type}' is not implemented yet")

    while True:
        if step.attempts >= settings.step_max_attempts:
            raise StepError(f"gave up after {step.attempts} attempts (a worker may have crashed while running it)")

        step.attempts += 1
        session.commit()
        try:
            return run(src, dst, step.params or {})
        except StepError:
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if step.attempts >= settings.step_max_attempts:
                raise StepError(f"failed after {step.attempts} attempts: {error}") from e
            delay = settings.retry_backoff_seconds * 2 ** (step.attempts - 1)
            log.warning("step_attempt_failed", attempt=step.attempts, error=error, retry_in_seconds=delay)
            time.sleep(delay)


def process_job(job_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        current = job.input_file

        for step in job.steps:
            if step.status == StepStatus.COMPLETED:
                current = step.output_file
                continue

            structlog.contextvars.bind_contextvars(step=step.step_type, step_index=step.step_index)

            job.current_step_index = step.step_index
            job.heartbeat_at = utcnow()
            step.status = StepStatus.RUNNING
            step.started_at = utcnow()
            step.input_file = current
            session.commit()
            log.info("step_started")

            src = full_path(current.storage_path)
            dst = full_path(f"work/{job.id}/{step.step_index}-{step.step_type}")
            dst.parent.mkdir(parents=True, exist_ok=True)

            try:
                result = run_step(session, step, src, dst)
            except Exception as e:
                step.status = StepStatus.FAILED
                step.error_message = str(e)
                step.completed_at = utcnow()
                for later in job.steps[step.step_index + 1:]:
                    later.status = StepStatus.SKIPPED
                job.status = JobStatus.FAILED
                job.error_message = f"step {step.step_index} ({step.step_type}) failed: {e}"
                job.completed_at = utcnow()
                session.commit()
                log.warning("step_failed", error=str(e))
                return

            if result != src:
                current = FileReference(
                    kind=FileKind.INTERMEDIATE,
                    storage_path=str(result.relative_to(settings.storage_root)),
                    size_bytes=result.stat().st_size,
                )
                session.add(current)

            step.output_file = current
            step.status = StepStatus.COMPLETED
            step.progress_percent = 100
            step.completed_at = utcnow()
            session.commit()
            log.info("step_completed", duration_ms=step.duration_ms)

        if current.kind == FileKind.INTERMEDIATE:
            current.kind = FileKind.OUTPUT
        job.output_file = current
        job.status = JobStatus.COMPLETED
        job.completed_at = utcnow()
        session.commit()
        log.info("job_completed")


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ping()
    log.info("worker_started", worker_id=WORKER_ID)

    while running:
        job_id = claim_next_job()
        if job_id is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(job_id=str(job_id), worker_id=WORKER_ID)
        log.info("job_claimed")
        heartbeat = Heartbeat(job_id)
        heartbeat.start()
        try:
            process_job(job_id)
        except Exception:
            log.exception("job_crashed")
        finally:
            heartbeat.stop()


if __name__ == "__main__":
    main()