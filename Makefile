.PHONY: db-up db-down migrate seed fe-install fe-dev fe-test fe-build \
        image image-ingest bm25 up down load

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

# --- F15 deployment ----------------------------------------------------------
# The ingestion image carries tesseract/libreoffice/ocrmypdf; the serving image deliberately
# does not. Ingestion and indexing are release STEPS, not prod runtime.
image-ingest:
	docker build -f docker/Dockerfile.ingestion -t campus-rag-ingest:local .

# Regenerate the release BM25 artifact. It is git-ignored (backend/app/data/*.pkl), so it must be
# produced rather than committed — and the serving image refuses to build without it, because a
# missing bm25.pkl makes /api/health report a core dependency down.
bm25: image-ingest
	docker run --rm --env-file backend/.env \
		-v "$$PWD/backend/app/data:/app/app/data" \
		campus-rag-ingest:local app.indexing.run --strategy structure --namespace all
	cp backend/app/data/bm25.pkl docker/bm25.pkl

image:
	@test -s docker/bm25.pkl || { \
	  echo "docker/bm25.pkl is missing or empty — run 'make bm25' first."; \
	  echo "The serving image bakes a release-matched BM25 index; /api/health treats a missing"; \
	  echo "one as a core dependency being down, so the build fails here rather than shipping"; \
	  echo "a container that boots straight into 503."; exit 1; }
	docker build -f docker/Dockerfile.serving \
		--build-arg APP_VERSION=$$(git rev-parse --short HEAD) \
		-t campus-rag-api:local .

# Thin alias — `docker compose -f docker/docker-compose.yml up --build` is the real command, and
# it works without make (which is not installed by default on Windows).
up:
	docker compose -f docker/docker-compose.yml up --build

down:
	docker compose -f docker/docker-compose.yml down

# LOAD_HOST=https://your-api.onrender.com make load
load:
	@test -n "$$LOAD_HOST" || { echo "set LOAD_HOST=<api base url>"; exit 1; }
	mkdir -p docs/loadtest/raw
	cd loadtest && locust -f locustfile.py --headless -u 50 -r 5 -t 5m \
		--host "$$LOAD_HOST" \
		--csv "../docs/loadtest/raw/$$(git rev-parse --short HEAD)"
