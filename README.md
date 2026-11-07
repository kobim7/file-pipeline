## פקודות ראשונות להרצה
cp .env.example .env        # first time only
make up                     # or: docker compose up --build -d
curl localhost:8000/health

## ממשק גרפי
API docs: http://localhost:8000/docs


## בדיקות

make test                   # or: docker compose exec api pytest -v

**test_health** (`tests/test_health.py`)  
What it does - Calls `/health`  
Expected - `200`, `status: ok`

**test_upload_creates_pending_job_and_stores_file** (`tests/test_upload.py`)  
What it does - Uploads a CSV with `validate` and `convert`  
Expected - `202`, a `pending` job with two `pending` steps, and one file in `inputs/`

**test_upload_rejects_bad_requests_before_storing_anything** (`tests/test_upload.py`)  
What it does - Sends 6 invalid requests  
Expected - `422` for an unknown step, an unknown param and an empty file; `415` for `.exe`; `413` for a file that's too large; `401` for a wrong key. No file is stored

**test_job_is_only_visible_to_its_owner** (`tests/test_upload.py`)  
What it does - Requests a job with another user's key  
Expected - `404` for the other user, `200` for the owner

**test_assignment_example_pipeline_runs_end_to_end** (`tests/test_pipeline.py`)  
What it does - Runs the example from the assignment: `validate`, `transform`, `convert` and `compress`  
Expected - `completed`, all 4 steps `completed`, and the compressed result contains only `name` and `email`

**test_filter_trim_and_case** (`tests/test_pipeline.py`)  
What it does - Filters `city = Haifa`, with `trim` and `upper`  
Expected - The result is `name,city` followed by `DANA,HAIFA`

**test_failed_step_fails_the_job_and_skips_the_rest** (`tests/test_pipeline.py`)  
What it does - Uploads a CSV with a missing value in a row  
Expected - `failed`, the message includes `line 2: expected 2 columns, got 1`, the step is `failed` and the next 3 are `skipped`, and the download returns `409`

**test_bad_input_is_not_retried** (`tests/test_pipeline.py`)  
What it does - Uses `select_columns` with a column that doesn't exist  
Expected - The step is `failed`, with `attempts = 1`

**test_transient_error_is_retried_until_it_succeeds** (`tests/test_pipeline.py`)  
What it does - A step that raises `OSError` twice, then succeeds  
Expected - `completed`, and the step ran 3 times

**test_transient_error_gives_up_after_max_attempts** (`tests/test_pipeline.py`)  
What it does - A step that always raises `OSError`  
Expected - `failed`, with `failed after 3 attempts: OSError: disk is full`

**test_job_from_crashed_worker_resumes_after_last_completed_step** (`tests/test_pipeline.py`)  
What it does - Simulates a dead worker: step 0 completed, step 1 in progress, last heartbeat 5 minutes ago  
Expected - `completed`, step 0 doesn't run again, and step 1 has `attempts = 2`

**test_csv_encodings_and_quoting** (`tests/test_pipeline.py`)  
What it does - `José` and `Müller`, a comma inside quotes and a line break inside a value, in UTF-8, UTF-8 with BOM and UTF-16, plus Windows-1252  
Expected - All 3 encodings convert to exactly the same JSON; Windows-1252 fails with `save it as UTF-8`, with `attempts = 1`

**test_semicolon_delimited_csv** (`tests/test_pipeline.py`)  
What it does - A CSV delimited with `;` and a value of `12,5`  
Expected - `[{"name": "Kobi", "price": "12,5"}, ...]`

**test_json_with_bom_and_utf16** (`tests/test_pipeline.py`)  
What it does - Converts to CSV a JSON file with a BOM and a JSON file in UTF-16  
Expected - Both give `name` followed by `José`

**test_broken_json_fails_without_retries_even_without_validate** (`tests/test_pipeline.py`)  
What it does - Truncated JSON, straight into `convert`  
Expected - `failed`, `invalid json`, with `attempts = 1`

**test_zip_extraction_picks_the_data_file** (`tests/test_pipeline.py`)  
What it does - 3 zip files: one CSV next to other files; 200 files with no data; two data files  
Expected - First: `[{"name": "Kobi"}]`. Second: `no csv or json files`, `and 190 more`, and a message shorter than 1000 characters. Third: `pick one with params.member`

**test_status_moves_from_pending_to_completed_with_step_details** (`tests/test_status_and_results.py`)  
What it does - Checks the status before and after processing  
Expected - Before: `pending`, with empty `started_at` and `result_url`. After: `completed`, `current_step_index = 2`, `result_url`, and every step has `progress_percent = 100` and `duration_ms`

**test_download_result_and_intermediate_results** (`tests/test_status_and_results.py`)  
What it does - Downloads before completion, after completion, an intermediate result, and a step that doesn't exist  
Expected - `409` before; `200` with `people.json.gz` after; the intermediate result is valid JSON; `404` for step 9

**test_expired_files_are_deleted_but_final_result_is_kept_longer** (`tests/test_status_and_results.py`)  
What it does - Moves the clock forward two hours and runs the cleanup  
Expected - The final result returns `200`, the intermediate file returns `410`

**test_cancel_pending_job** (`tests/test_status_and_results.py`)  
What it does - Cancels a waiting job, runs the worker, and cancels again  
Expected - `cancelled`, all 3 steps `skipped`, the worker doesn't pick it up, and the second cancel returns `409`


## Example API calls

Upload a CSV and convert it to JSON.

curl -s -X POST localhost:8000/jobs -H "X-API-Key: kobi" \
  -F "file=@contacts.csv" \
  -F 'pipeline=[{"step":"validate"},{"step":"convert","params":{"output_format":"json"}}]' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"


Get the full status of the job and each of its steps:
curl -s localhost:8000/jobs/$JOB -H "X-API-Key: kobi" | python3 -m json.tool


Get a one-line status summary:
curl -s localhost:8000/jobs/$JOB -H "X-API-Key: kobi" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], [(s['step_type'], s['status']) for s in d['steps']], d['error_message'] or '')"


Download the final result, saved under the name the server sends (for example `contacts.json`):
curl -s -OJ localhost:8000/jobs/$JOB/result -H "X-API-Key: kobi"


Download the output of a single step (an intermediate result). Steps are numbered from 0:
curl -s localhost:8000/jobs/$JOB/steps/0/result -H "X-API-Key: kobi"


Cancel a job that is waiting or running:
curl -s -X POST localhost:8000/jobs/$JOB/cancel -H "X-API-Key: kobi"


Run the full example pipeline from the assignment: validate, keep two columns, convert to JSON, and compress:
curl -s -X POST localhost:8000/jobs -H "X-API-Key: kobi" \
  -F "file=@contacts.csv" \
  -F 'pipeline=[{"step":"validate","params":{"expected_type":"csv"}},{"step":"transform","params":{"select_columns":["name","email"]}},{"step":"convert","params":{"output_format":"json"}},{"step":"compress","params":{"algorithm":"gzip"}}]' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"


View a compressed result without saving it:
curl -s localhost:8000/jobs/$JOB/result -H "X-API-Key: kobi" | gunzip


Convert a JSON file to CSV:
```bash
curl -s -X POST localhost:8000/jobs -H "X-API-Key: kobi" \
  -F "file=@contacts.json" \
  -F 'pipeline=[{"step":"validate"},{"step":"convert","params":{"output_format":"csv"}}]' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"
```

Unzip an archive that holds one CSV (other non-data files are ignored), then validate and convert it:
curl -s -X POST localhost:8000/jobs -H "X-API-Key: kobi" \
  -F "file=@export.zip" \
  -F 'pipeline=[{"step":"decompress"},{"step":"validate"},{"step":"convert","params":{"output_format":"json"}}]' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"


Unzip an archive that holds several data files, picking one of them by name:
curl -s -X POST localhost:8000/jobs -H "X-API-Key: kobi" \
  -F "file=@export.zip" \
  -F 'pipeline=[{"step":"decompress","params":{"member":"customers.csv"}},{"step":"validate"}]' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"


Decompress a gzip file (`.csv.gz` becomes `.csv`) and validate it:
curl -s -X POST localhost:8000/jobs -H "X-API-Key: kobi" \
  -F "file=@contacts.csv.gz" \
  -F 'pipeline=[{"step":"decompress"},{"step":"validate"}]' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"


An unknown parameter is rejected at upload time with `422`, and the message lists the allowed ones:
curl -s -X POST localhost:8000/jobs -H "X-API-Key: kobi" \
  -F "file=@contacts.csv" \
  -F 'pipeline=[{"step":"transform","params":{"columns":["name"]}}]'


A wrong API key is rejected with `401`:
curl -s -X POST localhost:8000/jobs -H "X-API-Key: wrong-key" \
  -F "file=@contacts.csv" \
  -F 'pipeline=[{"step":"validate"}]'



## Implemented steps

validate > transform > convert > compress
