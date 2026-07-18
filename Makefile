.PHONY: db-up db-down migrate seed fe-install fe-dev fe-test fe-build

db-up:
	docker compose -f docker/docker-compose.yml up -d postgres redis

db-down:
	docker compose -f docker/docker-compose.yml down

migrate:
	cd backend && alembic upgrade head

seed:
	cd backend && python -m app.db.seed

# F14 frontend. `fe-dev` proxies /api and /internal to VITE_API_BASE_URL, so the browser sees one
# origin — which is what keeps the SameSite=Lax anonymous session cookie working in dev.
fe-install:
	cd frontend && npm ci

fe-dev:
	cd frontend && npm run dev

fe-test:
	cd frontend && npm run lint && npm run typecheck && npm run test

fe-build:
	cd frontend && npm run build
