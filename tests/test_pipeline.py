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


def test_csv_encodings_and_quoting(client):
    convert = [{"step": "convert", "params": {"output_format": "json"}}]
    # Non-ASCII letters, a comma inside quotes, and a line break inside quotes.
    text = "name,note\nJosé,\"Zürich, Switzerland\"\nMüller,\"first line\nsecond line\"\n"
    expected = [
        {"name": "José", "note": "Zürich, Switzerland"},
        {"name": "Müller", "note": "first line\nsecond line"},
    ]
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):  # utf-8-sig = Excel's "CSV UTF-8"
        job_id = upload(client, convert, content=text.encode(encoding)).json()["id"]
        run_worker()
        r = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
        assert r.json() == expected, encoding

    # A legacy Windows encoding without a BOM can't be detected, so it must be rejected.
    legacy = upload(client, [{"step": "validate"}], content=text.encode("cp1252")).json()["id"]
    run_worker()
    job = get_job(client, legacy)
    assert job["status"] == "failed"
    assert "save it as UTF-8" in job["error_message"]
    with SessionLocal() as session:
        assert session.get(Job, uuid.UUID(legacy)).steps[0].attempts == 1  # not retried


def test_semicolon_delimited_csv(client):
    # European Excel writes ';' because ',' is the decimal separator there.
    content = "name;price\nKobi;12,5\nDana;7\n".encode()
    job_id = upload(client, [{"step": "convert", "params": {"output_format": "json"}}], content=content).json()["id"]
    run_worker()

    r = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
    assert r.json() == [{"name": "Kobi", "price": "12,5"}, {"name": "Dana", "price": "7"}]


def test_json_with_bom_and_utf16(client):
    to_csv = [{"step": "validate"}, {"step": "convert", "params": {"output_format": "csv"}}]
    body = json.dumps([{"name": "José"}], ensure_ascii=False)
    for encoding in ("utf-8-sig", "utf-16"):
        job_id = upload(client, to_csv, content=body.encode(encoding), filename="people.json").json()["id"]
        run_worker()
        r = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
        assert r.text.splitlines() == ["name", "José"], encoding


def test_broken_json_fails_without_retries_even_without_validate(client):
    pipeline = [{"step": "convert", "params": {"output_format": "csv"}}]
    job_id = upload(client, pipeline, content=b'[{"n": 1}, {"n":', filename="broken.json").json()["id"]
    run_worker()

    job = get_job(client, job_id)
    assert job["status"] == "failed"
    assert "invalid json" in job["error_message"]
    with SessionLocal() as session:
        assert session.get(Job, uuid.UUID(job_id)).steps[0].attempts == 1


def _zip(files: dict) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_zip_extraction_picks_the_data_file(client):
    pipeline = [{"step": "decompress"}, {"step": "convert", "params": {"output_format": "json"}}]

    # One csv next to other files: picked automatically.
    content = _zip({"data/people.csv": "name\nKobi\n", "README.txt": "hi", "__MACOSX/._people.csv": "x"})
    job_id = upload(client, pipeline, content=content, filename="export.zip").json()["id"]
    run_worker()
    assert client.get(f"/jobs/{job_id}/result", headers=HEADERS).json() == [{"name": "Kobi"}]

    # Not a data archive at all (say, an app installer): clear, short error.
    app = _zip({f"App.app/Contents/Resources/{i}.lproj/Localizable.strings": "x" for i in range(200)})
    job_id = upload(client, pipeline, content=app, filename="installer.zip").json()["id"]
    run_worker()
    error = get_job(client, job_id)["error_message"]
    assert "no csv or json files" in error
    assert "and 190 more" in error
    assert len(error) < 1000

    # Two data files: ask the user to pick.
    two = _zip({"a.csv": "x\n1\n", "b.json": "[]"})
    job_id = upload(client, pipeline, content=two, filename="two.zip").json()["id"]
    run_worker()
    assert "pick one with params.member" in get_job(client, job_id)["error_message"]


def test_tab_delimited_txt_file(client):
    # Excel's "Text (Tab delimited)" export.
    content = b"name\tcity\nKobi\tHaifa\nDana\tTel Aviv\n"
    pipeline = [{"step": "validate"}, {"step": "convert", "params": {"output_format": "json"}}]
    job_id = upload(client, pipeline, content=content, filename="people.txt").json()["id"]
    run_worker()

    r = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
    assert r.json() == [{"name": "Kobi", "city": "Haifa"}, {"name": "Dana", "city": "Tel Aviv"}]

    # transform keeps it a tab-delimited .txt file
    job_id = upload(client, [{"step": "transform", "params": {"case": "upper"}}],
                    content=content, filename="people.txt").json()["id"]
    run_worker()
    r = client.get(f"/jobs/{job_id}/result", headers=HEADERS)
    assert 'filename="people.txt"' in r.headers["content-disposition"]
    assert r.text.splitlines()[1] == "KOBI\tHAIFA"


def test_uploaded_gz_keeps_the_inner_file_type(client):
    import gzip

    pipeline = [{"step": "decompress"}, {"step": "convert", "params": {"output_format": "json"}}]
    job_id = upload(client, pipeline, content=gzip.compress(b"name\nKobi\n"), filename="people.csv.gz").json()["id"]
    run_worker()
    assert client.get(f"/jobs/{job_id}/result", headers=HEADERS).json() == [{"name": "Kobi"}]

    # A .gz with no inner extension: a clear error instead of a confusing one.
    job_id = upload(client, pipeline, content=gzip.compress(b"name\nKobi\n"), filename="people.gz").json()["id"]
    run_worker()
    assert "name it like data.csv.gz" in get_job(client, job_id)["error_message"]
