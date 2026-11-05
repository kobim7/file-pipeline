# File Processing Pipeline

FastAPI API + background workers + Postgres (metadata and job queue) + local disk storage (shared Docker volume).

## Run

```bash
cp .env.example .env        # first time only
make up                     # or: docker compose up --build -d
curl localhost:8000/health
```

API docs: http://localhost:8000/docs

## Tests

```bash
make test                   # or: docker compose exec api pytest -v
```

## Example API calls

_TODO once endpoints exist._

## Implemented steps

_TODO: validate / transform / convert / compress / notify._
