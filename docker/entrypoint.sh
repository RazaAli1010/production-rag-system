#!/bin/sh
# CampusRAG serving entrypoint (F15, AC-8).
set -e   # a failed migration exits non-zero here, so uvicorn never starts on a half-migrated DB

echo "campus-rag: APP_VERSION=${APP_VERSION:-dev} — running migrations"
alembic upgrade head

# `exec` so uvicorn becomes PID 1 and receives SIGTERM directly. Without it the shell holds PID 1,
# swallows the signal, and the platform SIGKILLs the container mid-stream — `_lifespan` never runs
# and the Redis pool is never closed.
#
# WEB_CONCURRENCY defaults to 1 on purpose: the F9 cache matrix and the BM25 index are per-process,
# so a second worker doubles resident memory and halves the cache hit rate. Raising it is a
# paid-tier decision — see docs/runbook.md.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-1}"
