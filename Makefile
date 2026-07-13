.PHONY: db-up db-down migrate seed

db-up:
	docker compose -f docker/docker-compose.yml up -d postgres redis

db-down:
	docker compose -f docker/docker-compose.yml down

migrate:
	cd backend && alembic upgrade head

seed:
	cd backend && python -m app.db.seed
