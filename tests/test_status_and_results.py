"""4. Job status tracking, 5. file retrieval, and cancellation."""
from datetime import timedelta

from app.cleanup import delete_expired_files
from app.db import SessionLocal
from app.models import FileReference
from tests.conftest import HEADERS, get_job, run_worker, upload

PIPELINE = [
    {"step": "validate"},
    {"step": "convert", "params": {"output_format": "json"}},
    {"step": "compress"},
]


def test_status_moves_from_pending_to_completed_with_step_details(client):
    job_id = upload(client, PIPELINE).json()["id"]

    before = get_job(client, job_id)
    assert before["status"] == "pending"
    assert before["started_at"] is None
    assert before["result_url"] is None

    run_worker()

    after = get_job(client, job_id)
    assert after["status"] == "completed"
    assert after["current_step_index"] == 2
    assert after["result_url"] == f"/jobs/{job_id}/result"
    for step in after["steps"]:
        assert step["status"] == "completed"
        assert step["progress_percent"] == 100
        assert step["duration_ms"] is not None


def test_download_result_and_intermediate_results(client):
    job_id = upload(client, PIPELINE).json()["id"]
    assert client.get(f"/jobs/{job_id}/result", headers=HEADERS).status_code == 409  # not ready yet

    run_worker()

    final = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
    assert final.status_code == 200
    assert 'filename="people.json.gz"' in final.headers["content-disposition"]

    intermediate = client.get(f"/jobs/{job_id}/steps/1/result", headers=HEADERS)
    assert intermediate.status_code == 200
    assert intermediate.json()[0] == {"name": "Kobi", "email": "k@x.com", "city": "Tel Aviv"}

    assert client.get(f"/jobs/{job_id}/steps/9/result", headers=HEADERS).status_code == 404


def test_expired_files_are_deleted_but_final_result_is_kept_longer(client):
    job_id = upload(client, PIPELINE).json()["id"]
    run_worker()

    # Jump two hours ahead: intermediates (1h) expire, the result (24h) doesn't.
    with SessionLocal() as session:
        for f in session.query(FileReference):
            f.expires_at -= timedelta(hours=2)
        session.commit()
    delete_expired_files()

    assert client.get(f"/jobs/{job_id}/result", headers=HEADERS).status_code == 200
    assert client.get(f"/jobs/{job_id}/steps/1/result", headers=HEADERS).status_code == 410


def test_cancel_pending_job(client):
    job_id = upload(client, PIPELINE).json()["id"]

    r = client.post(f"/jobs/{job_id}/cancel", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    run_worker()  # the worker must not pick it up

    job = get_job(client, job_id)
    assert job["status"] == "cancelled"
    assert [s["status"] for s in job["steps"]] == ["skipped"] * 3
    assert client.post(f"/jobs/{job_id}/cancel", headers=HEADERS).status_code == 409
