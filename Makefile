.PHONY: up down logs test shell psql reset

up:        ## build and start everything
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

test:      ## run tests inside the api container
	docker compose exec api pytest -v

shell:
	docker compose exec api bash

psql:
	docker compose exec db psql -U pipeline -d pipeline

reset:     ## wipe DB + stored files
	docker compose down -v
