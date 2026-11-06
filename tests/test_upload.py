"""1. File upload and job creation."""
from pathlib import Path

from app.config import settings
from tests.conftest import HEADERS, get_job, upload


def test_upload_creates_pending_job_and_stores_file(client):
    r = upload(client, [{"step": "validate"}, {"step": "convert", "params": {"output_format": "json"}}])

    assert r.status_code == 202
    job_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    job = get_job(client, job_id)
    assert [s["step_type"] for s in job["steps"]] == ["validate", "convert"]
    assert all(s["status"] == "pending" for s in job["steps"])

    stored = list((Path(settings.storage_root) / "inputs").iterdir())
    assert len(stored) == 1
    assert stored[0].read_bytes().startswith(b"name,email,city")


def test_upload_rejects_bad_requests_before_storing_anything(client, monkeypatch):
    # unknown step, unknown param, unsupported file type, empty file, file too large, bad key
    assert upload(client, [{"step": "magic"}]).status_code == 422
    r = upload(client, [{"step": "transform", "params": {"columns": ["name"]}}])
    assert r.status_code == 422
    assert "select_columns" in r.json()["detail"]
    assert upload(client, [{"step": "validate"}], filename="virus.exe").status_code == 415
    assert upload(client, [{"step": "validate"}], content=b"").status_code == 422

    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    assert upload(client, [{"step": "validate"}]).status_code == 413

    assert upload(client, [{"step": "validate"}], headers={"X-API-Key": "wrong"}).status_code == 401

    inputs = Path(settings.storage_root) / "inputs"
    assert not inputs.exists() or not any(inputs.iterdir())


def test_job_is_only_visible_to_its_owner(client, monkeypatch):
    job_id = upload(client, [{"step": "validate"}]).json()["id"]
    monkeypatch.setattr(settings, "api_keys", "test-key,someone-else")

    assert client.get(f"/jobs/{job_id}", headers={"X-API-Key": "someone-else"}).status_code == 404
    assert client.get(f"/jobs/{job_id}", headers=HEADERS).status_code == 200
