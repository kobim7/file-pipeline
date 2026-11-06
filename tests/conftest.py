"""Shared test setup.

Tests run against a throwaway SQLite database and a temp storage folder, never the
dev Postgres or the real /data volume. The env vars must be set before any `app`
module is imported, because settings are read at import time.
"""
import os
import shutil
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="pipeline-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'test.db'}"
os.environ["STORAGE_ROOT"] = str(TEST_ROOT / "storage")
os.environ["API_KEYS"] = "test-key"
os.environ["RETRY_BACKOFF_SECONDS"] = "0"

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.worker import claim_next_job, process_job  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}
PEOPLE_CSV = b"name,email,city\nKobi,k@x.com,Tel Aviv\nDana,d@x.com,Haifa\nRon,r@x.com,Haifa\n"


@pytest.fixture(autouse=True)
def clean_state():
    """Every test starts with empty tables and an empty storage folder."""
    db.Base.metadata.drop_all(db.engine)
    db.Base.metadata.create_all(db.engine)
    storage = Path(settings.storage_root)
    shutil.rmtree(storage, ignore_errors=True)
    storage.mkdir(parents=True)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def upload(client, pipeline, content=PEOPLE_CSV, filename="people.csv", headers=HEADERS):
    return client.post(
        "/jobs",
        headers=headers,
        files={"file": (filename, content)},
        data={"pipeline": json.dumps(pipeline)},
    )


def run_worker():
    """Process every pending job, the way a worker would, then return."""
    while (job_id := claim_next_job()) is not None:
        process_job(job_id)


def get_job(client, job_id):
    return client.get(f"/jobs/{job_id}", headers=HEADERS).json()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
