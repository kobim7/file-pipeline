"""2. Pipeline execution end-to-end, and 3. step failure handling."""
import gzip
import json
import uuid
from datetime import timedelta

from app import steps
from app.db import SessionLocal
from app.models import Job, JobStatus, StepStatus, utcnow
from tests.conftest import HEADERS, get_job, run_worker, upload

ASSIGNMENT_EXAMPLE = [
    {"step": "validate", "params": {"expected_type": "csv"}},
    {"step": "transform", "params": {"select_columns": ["name", "email"]}},
    {"step": "convert", "params": {"output_format": "json"}},
    {"step": "compress", "params": {"algorithm": "gzip"}},
]


def test_assignment_example_pipeline_runs_end_to_end(client):
    job_id = upload(client, ASSIGNMENT_EXAMPLE).json()["id"]
    run_worker()

    job = get_job(client, job_id)
    assert job["status"] == "completed"
    assert [s["status"] for s in job["steps"]] == ["completed"] * 4

    r = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
    records = json.loads(gzip.decompress(r.content))
    assert records == [
        {"name": "Kobi", "email": "k@x.com"},
        {"name": "Dana", "email": "d@x.com"},
        {"name": "Ron", "email": "r@x.com"},
    ]


def test_filter_trim_and_case(client):
    csv = b"name,city\n  kobi ,Tel Aviv\n dana,Haifa\n"
    pipeline = [{"step": "transform", "params": {
        "filter": {"column": "city", "value": "Haifa"}, "trim": True, "case": "upper",
    }}]
    job_id = upload(client, pipeline, content=csv).json()["id"]
    run_worker()

    r = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
    assert r.text.splitlines() == ["name,city", "DANA,HAIFA"]


def test_failed_step_fails_the_job_and_skips_the_rest(client):
    broken = b"name,city\nKobi\n"
    job_id = upload(client, ASSIGNMENT_EXAMPLE, content=broken).json()["id"]
    run_worker()

    job = get_job(client, job_id)
    assert job["status"] == "failed"
    assert "line 2: expected 2 columns, got 1" in job["error_message"]
    assert [s["status"] for s in job["steps"]] == ["failed", "skipped", "skipped", "skipped"]
    assert client.get(f"/jobs/{job_id}/result", headers=HEADERS).status_code == 409


def test_bad_input_is_not_retried(client):
    job_id = upload(client, [{"step": "transform", "params": {"select_columns": ["nope"]}}]).json()["id"]
    run_worker()

    with SessionLocal() as session:
        step = session.get(Job, uuid.UUID(job_id)).steps[0]
        assert step.status == StepStatus.FAILED
        assert step.attempts == 1


def test_transient_error_is_retried_until_it_succeeds(client, monkeypatch):
    calls = []

    def flaky_validate(src, dst, params):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("disk hiccup")
        return src

    monkeypatch.setitem(steps.STEPS, "validate", flaky_validate)
    job_id = upload(client, [{"step": "validate"}]).json()["id"]
    run_worker()

    job = get_job(client, job_id)
    assert job["status"] == "completed"
    assert len(calls) == 3


def test_transient_error_gives_up_after_max_attempts(client, monkeypatch):
    def always_broken(src, dst, params):
        raise OSError("disk is full")

    monkeypatch.setitem(steps.STEPS, "validate", always_broken)
    job_id = upload(client, [{"step": "validate"}]).json()["id"]
    run_worker()

    job = get_job(client, job_id)
    assert job["status"] == "failed"
    assert "failed after 3 attempts: OSError: disk is full" in job["error_message"]


def test_job_from_crashed_worker_resumes_after_last_completed_step(client, monkeypatch):
    job_id = upload(client, ASSIGNMENT_EXAMPLE).json()["id"]

    # Simulate a worker that finished step 0, started step 1, then died.
    with SessionLocal() as session:
        job = session.get(Job, uuid.UUID(job_id))
        job.status = JobStatus.PROCESSING
        job.worker_id = "dead-worker"
        job.heartbeat_at = utcnow() - timedelta(minutes=5)
        job.steps[0].status = StepStatus.COMPLETED
        job.steps[0].output_file = job.input_file
        job.steps[0].attempts = 1
        job.steps[1].status = StepStatus.RUNNING
        job.steps[1].attempts = 1
        session.commit()

    ran = []
    real_validate = steps.STEPS["validate"]
    monkeypatch.setitem(steps.STEPS, "validate", lambda *a: ran.append("validate") or real_validate(*a))
    run_worker()

    job = get_job(client, job_id)
    assert job["status"] == "completed"
    assert ran == []  # step 0 was already done, so it didn't run again
    with SessionLocal() as session:
        assert session.get(Job, uuid.UUID(job_id)).steps[1].attempts == 2
