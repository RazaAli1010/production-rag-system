# F15 — Deployment, Docker, CI/CD & Load Testing · requirements.md

**Module:** `docker/`, `.github/workflows/`, `loadtest/`, repo-root `README.md` ·
**Phase:** D (ship) · **Depends on:** F11 (health, error envelope, rate limit), F13 (structlog +
`request_logs` + `/internal/stats`), F14 (frontend build).

F15 adds **no new pipeline behaviour, no Alembic migration, and no retrieval code**. It packages
what exists and proves it survives 50 concurrent users. It adds exactly **three** Settings keys
(§6) and **one** new route (`GET /api/health/live`, §5.2) — both are justified below against a
concrete failure mode, not added speculatively.

---

## 1. What already exists (read from the repo, not the brief)

Where this section and the feature brief disagree, the repo wins and the disagreement is called out.

| Thing | State today |
|---|---|
| `docker/docker-compose.yml` | postgres:16 + redis:7 only. **No `api` service, no Dockerfile anywhere in the repo.** |
| `Makefile` | `db-up`, `db-down`, `migrate`, `seed`, `fe-*`. No image targets. |
| `.github/workflows/ci.yml` | 9 per-module Python jobs (db, ingestion, indexing, rag, caching, memory, evals, auth, api). Each already runs Postgres as a service container, `alembic upgrade head`, its own pytest subset, and a per-module async guard. |
| `.github/workflows/frontend.yml` | lint + typecheck + vitest + build + font budget + no-raw-HTML guard + Lighthouse, `paths:`-filtered to `frontend/**`. |
| Lint/types | `ruff` in `[dev]`. **`mypy` is not installed and has never run.** |
| `GET /api/health` | Probes postgres, redis, pinecone, bm25, openai key concurrently; **returns 503 when any core dep is down.** |
| `request_logs` | Already carries `total_ms`, `cache_hit`, `refused`, `degraded`, `http_status`, `error_type`, per-stage ms, tokens, `est_cost_usd`. |
| `/internal/stats?window=…` | Admin-only aggregation over `request_logs` (F13). |
| Anonymous session cookie | `app/api/sessions.py` sets it `httponly=True, samesite="lax"`, **no `secure` flag**. |
| `backend/app/data/*.pkl` | **git-ignored.** `bm25.pkl` is not in the repo and cannot simply be `COPY`d. |
| Repo-root `README.md` | **Does not exist.** F15 owns creating it. |
| Locust | Not a dependency, no `loadtest/` directory. |

### 1.1 Three facts that shape the whole design

1. **`/api/health` returning 503 makes it unusable as a platform health check.** Render restarts an
   instance whose health check fails. Redis is a *fail-open* dependency (`redis_hot` short-circuits
   to a miss) and an Upstash blip would therefore restart an API that is still perfectly able to
   answer questions. The deep probe stays as-is for humans and monitoring; the platform gets a
   dependency-free liveness route (AC-16).
2. **`bm25.pkl` is git-ignored, and `/api/health` counts a missing BM25 index as *core down*.** An
   image built naively is DOA. The artifact must be produced by the ingestion image and injected
   into the serving build context by the release workflow (AC-6, AC-7).
3. **Vercel frontend + Render API are cross-site**, so a `SameSite=Lax` cookie is never sent on
   `POST /api/sessions` from the browser and every anonymous session silently breaks in prod —
   a failure that cannot reproduce locally, where the Vite proxy makes everything same-origin.
   Fixed by making the cookie attributes configurable (AC-19).

---

## 2. User stories

- **US-1 (contributor).** As a contributor, I run `docker compose up` once and get API + Postgres +
  Redis with migrations already applied, so I can ask a question in under five minutes from a clean
  clone.
- **US-2 (reviewer).** As a reviewer, I see one CI run per PR that lints, type-checks, migrates,
  tests both halves of the monorepo, and proves the Docker image still builds, so `main` is always
  releasable.
- **US-3 (operator).** As the operator, I push a tag and the backend image builds, publishes to
  GHCR and deploys to Render without me touching a console, and I can tell from `/api/health/live`
  exactly which release is serving traffic.
- **US-4 (operator, 3am).** As the operator paged at 3am, I open one runbook and find the exact
  commands to rotate a key, flush the cache, roll a migration back, re-index after a corpus change,
  and read the trace of a slow request.
- **US-5 (hiring reviewer).** As someone evaluating this project, I read the README and see the
  architecture, the baseline→F9 benchmark table, and the measured load-test numbers, without
  running anything.
- **US-6 (me, before launch).** As the author, I run one Locust command against the deployed URL
  and learn the real p50/p95/p99, error rate, and cache hit rate at 50 concurrent users — including
  multi-turn session traffic — so the README quotes a measurement, not a hope.

---

## 3. Acceptance criteria (EARS)

### Serving image

- **AC-1.** WHEN `docker build -f docker/Dockerfile.serving .` is run from the repo root, THE
  SYSTEM SHALL produce a runnable image from `python:3.11-slim` using a multi-stage build in which
  build-only tooling (compilers, pip cache, `.[dev]`) is absent from the final stage.
- **AC-2.** THE serving image SHALL install torch from the PyTorch CPU index
  (`--index-url https://download.pytorch.org/whl/cpu`) and SHALL NOT contain any CUDA runtime
  library.
- **AC-3.** WHEN the image is built, THE SYSTEM SHALL pre-download the
  `cross-encoder/ms-marco-MiniLM-L-6-v2` weights into the image, and the runtime stage SHALL set
  `HF_HUB_OFFLINE=1` so no request path ever reaches huggingface.co.
- **AC-4.** THE built serving image SHALL be ≤ 2.5 GB uncompressed, and CI SHALL fail the build if
  it is larger.
- **AC-5.** THE serving image SHALL NOT contain tesseract, libreoffice, ocrmypdf, or any other
  ingestion-only system package.
- **AC-6.** WHEN the release workflow builds the serving image, THE SYSTEM SHALL place a
  release-matched `bm25.pkl` at `app/data/bm25.pkl` inside the image.
- **AC-7.** IF `bm25.pkl` is absent from the build context, THEN the serving image build SHALL fail
  with an explicit message naming the artifact, rather than producing an image that boots and then
  reports `bm25: missing` from `/api/health`.
- **AC-8.** WHEN a container starts, THE entrypoint SHALL run `alembic upgrade head` and SHALL
  start uvicorn only if the migration succeeds; IF the migration fails, THEN the container SHALL
  exit non-zero without serving traffic.
- **AC-9.** THE image SHALL declare a `HEALTHCHECK` against `GET /api/health/live` (not
  `/api/health` — see AC-16).
- **AC-10.** THE serving container SHALL run as a non-root user.

### Ingestion image

- **AC-11.** THE ingestion image SHALL extend the serving dependency set with `tesseract-ocr`
  (incl. `eng`+`urd` language packs), `libreoffice`, and `ocrmypdf`, and SHALL be usable to run
  `python -m app.ingestion.run` and `python -m app.indexing.run`.
- **AC-12.** THE ingestion image SHALL NOT be deployed to Render or referenced by
  `docker-compose.yml`'s default profile; it is a local/CI release tool only.

### Local stack

- **AC-13.** WHEN `docker compose -f docker/docker-compose.yml up` is run with a populated `.env`,
  THE SYSTEM SHALL start api + postgres + redis, apply migrations automatically, and serve
  `GET /api/health` on `localhost:8000` within 90 s.
- **AC-14.** THE api service SHALL wait on the postgres and redis health checks
  (`depends_on: condition: service_healthy`) before starting.
- **AC-15.** WHILE the stack is running, `make db-up` / `make migrate` / `make seed` SHALL continue
  to work unchanged against the same containers.

### Health & liveness

- **AC-16.** THE SYSTEM SHALL expose `GET /api/health/live` returning `200` with
  `{"status":"live","version":<APP_VERSION>}` whenever the process is running, touching **no**
  external dependency and making no billable call.
- **AC-17.** `GET /api/health` SHALL keep its current behaviour (per-dependency detail, 503 when a
  core dependency is down) and SHALL remain the probe used by the runbook and by monitoring.
- **AC-18.** THE deployed API SHALL report `APP_VERSION` equal to the git SHA or tag of the image
  actually serving traffic.

### Cross-site production topology

- **AC-19.** WHEN `COOKIE_SAMESITE=none` and `COOKIE_SECURE=true` are configured, THE anonymous
  session cookie SHALL be issued with `SameSite=None; Secure`, so a browser on the Vercel origin
  sends it to the Render origin; the defaults (`lax` / `false`) SHALL preserve today's local
  behaviour exactly.
- **AC-20.** THE deployed API SHALL set `CORS_ALLOW_ORIGINS` to the exact Vercel origin(s) — never
  a wildcard — and SHALL respond to a browser `POST /api/ask` from that origin with the SSE stream
  intact.
- **AC-21.** WHEN the deployed frontend is served from Vercel, `VITE_API_BASE_URL` SHALL point at
  the Render API origin and the production bundle SHALL contain no `localhost` references.

### CI — pull requests

- **AC-22.** WHEN a PR is opened, THE SYSTEM SHALL run `ruff check` over the whole of
  `backend/app` and `backend/tests` (today only per-module subsets are linted) and fail on any
  finding.
- **AC-23.** WHEN a PR is opened, THE SYSTEM SHALL run `mypy` in basic mode over `backend/app`
  and fail on error; the configuration SHALL be committed in `backend/pyproject.toml` with any
  module-level ignores listed explicitly rather than a blanket `ignore_errors`.
- **AC-24.** WHEN a PR is opened, THE SYSTEM SHALL run a job that starts Postgres **and Redis**
  service containers, runs `alembic upgrade head`, then `alembic downgrade base`, then
  `alembic upgrade head` again, and fails if any step errors — proving the rollback path in the
  runbook actually works.
- **AC-25.** WHEN a PR is opened, THE SYSTEM SHALL fail if `alembic revision --autogenerate`
  produces a non-empty migration against the committed models (the F11 no-drift guard, generalised
  to all of `app/db/models`).
- **AC-26.** WHEN a PR touches `backend/**`, `docker/**`, or the release workflow, THE SYSTEM SHALL
  build the serving image (without pushing) and fail on build error or on breaching the 2.5 GB
  budget of AC-4.
- **AC-27.** THE existing nine per-module jobs and the frontend workflow SHALL continue to run
  unchanged; F15 SHALL NOT fold them into one job.

### CI — release

- **AC-28.** WHEN a tag matching `v*` is pushed, THE SYSTEM SHALL build and push the serving image
  to GHCR tagged with both the tag and the git SHA.
- **AC-29.** WHEN the GHCR push succeeds, THE SYSTEM SHALL call the Render deploy hook
  (`RENDER_DEPLOY_HOOK_URL` secret) and SHALL fail the workflow if the hook returns non-2xx.
- **AC-30.** WHEN the deploy hook has been called, THE SYSTEM SHALL poll `GET /api/health/live` on
  the public URL until it reports the new `APP_VERSION` or a 10-minute timeout elapses, and SHALL
  fail the workflow on timeout.
- **AC-31.** THE release workflow SHALL NOT run ingestion or indexing against production
  automatically; a corpus change is a deliberate runbook step (AC-40).
- **AC-32.** IF any required secret (`RENDER_DEPLOY_HOOK_URL`, GHCR token) is missing, THEN the
  release workflow SHALL fail with a message naming the secret, not silently skip the deploy.

### CI — nightly (stretch)

- **AC-33.** WHEN the nightly schedule fires AND the `OPENAI_API_KEY`/`PINECONE_API_KEY` secrets
  are present, THE SYSTEM SHALL run the F4 retrieval suite over a 10-question smoke subset and open
  an issue on regression beyond a committed tolerance; IF the secrets are absent, THEN the job
  SHALL skip cleanly rather than fail.
- **AC-34.** THE nightly job SHALL NOT write an eval label into `docs/eval_results/` — the fixed
  label sequence stays owned by the manual eval gates.

### Load test

- **AC-35.** THE SYSTEM SHALL ship a Locust file at `loadtest/locustfile.py` driving `POST
  /api/ask` at 50 concurrent users with an **80% repeat / 20% novel** question mix, a mix of
  authenticated and anonymous users, and **~30% of users running 3-turn session conversations**
  (create session → three asks reusing `session_id`), exercising F17 memory reads and writes.
- **AC-36.** THE Locust client SHALL consume the SSE stream to `done` (or `error`) and SHALL count
  a run as failed if `done` never arrives — a load test that measures time-to-first-byte on a
  streaming endpoint measures nothing.
- **AC-37.** WHEN a load test completes, THE SYSTEM SHALL record client-side p50/p95/p99, error
  rate, and RPS from Locust, AND server-side cache hit rate and per-stage timings read from
  `request_logs` / `GET /internal/stats` for the run window — every number quoted in the README
  SHALL come from one of those two sources.
- **AC-38.** THE run SHALL be committed to `docs/loadtest/` as a report containing the git SHA, the
  flag configuration, the instance tier, and the raw Locust CSVs.
- **AC-39.** THE load test SHALL meet **p95 < 4 s at 50 users cache-warm**, OR the report SHALL
  contain a bottleneck analysis naming the dominant stage from the `request_logs` per-stage
  columns and the remediation considered.

### Runbook & README

- **AC-40.** THE runbook (`docs/runbook.md`) SHALL document, with copy-pasteable commands: key
  rotation (OpenAI/Pinecone/JWT — including that rotating `JWT_SECRET` invalidates every live
  access token and that `refresh_tokens` is the blacklist), cache flush
  (`python -m app.caching.run --flush`), the corpus update → re-ingest → re-index → cache
  invalidation → bm25 rebuild → redeploy sequence, migration rollback (`alembic downgrade -1`),
  and how to read a Langfuse trace from a `request_id`.
- **AC-41.** THE runbook SHALL document the Render free-tier cold start (spin-down after inactivity,
  first-request latency) with the chosen mitigation stated as a decision — either an uptime pinger
  with its cron, or an explicit "accepted, documented" — and SHALL note that the F14 UI's first
  request may therefore appear to hang.
- **AC-42.** THE repo-root `README.md` SHALL contain: a one-paragraph product statement, an
  architecture diagram, a quickstart proven to work from a clean clone, the full
  `baseline → F5 → F6 → F7 → F8 → F9` benchmark table with each row linked to its
  `docs/eval_results/` report, the load-test table from AC-38, the JD keyword stack table, and the
  honest default-off status of `ENABLE_QUERY_REWRITE` / `ENABLE_CACHE`.
- **AC-43.** EVERY benchmark number in the README SHALL match a committed report in
  `docs/eval_results/`; no number SHALL appear in the README that does not exist in a report.

### Non-functional

- **AC-44.** F15 SHALL add no Alembic migration; CI SHALL assert this (AC-25 covers it).
- **AC-45.** Every new configuration value SHALL live in the central `Settings` class (§6); no
  `os.environ` read SHALL be introduced in `app/`.
- **AC-46.** THE deployment SHALL be reversible: redeploying the previous GHCR tag SHALL restore
  the previous release without a database change, and the runbook SHALL state the one case where
  this is false (a release that ran a forward migration → `alembic downgrade` first).

---

## 4. Non-functional requirements

- **NFR-1 — Memory ceiling.** Render's free tier gives 512 MB. torch + a resident cross-encoder +
  the F9 in-process embedding matrix (`CACHE_MAX_ENTRIES=10_000` ⇒ 61 MB) plausibly exceed it.
  Container RSS after warmup MUST be **measured** (T14) and recorded; if it exceeds ~450 MB the
  deployment ships with `ENABLE_RERANK=false` or on a paid tier, and the README says which. This is
  a measurement, not an assumption.
- **NFR-2 — Cold start.** Rerank warmup is already backgrounded in `_lifespan`, so readiness does
  not block on the ~19 s weight load. The platform health check must therefore be the liveness
  route, or the first boot fails its own probe.
- **NFR-3 — Secrets.** No secret is ever baked into an image or printed by CI. `.env.example`
  documents every key with placeholder values only.
- **NFR-4 — CI wall time.** The added jobs must not push a PR run past ~15 minutes; the Docker
  build job is `paths:`-filtered (AC-26) and uses layer caching.

---

## 5. Design constraints inherited from Shared Context

### 5.1 Contracts untouched

`AnswerResponse`, `Citation`, `StageEvent`, the SSE event order (`stage*` → `token*` → `citations`
→ `meta` → `done`|`error`), and the F3 retriever seam are consumed by the Locust client and by
nothing else in F15. The load test asserts against the wire contract; it does not extend it.

### 5.2 The one new route

`GET /api/health/live` is additive, unauthenticated, dependency-free, and lives in the existing
`app/api/health.py`. It does not alter `GET /api/health`. Rationale in §1.1(1).

---

## 6. New Settings keys (the complete list)

| Key | Type | Default | Why it must exist |
|---|---|---|---|
| `APP_VERSION` | `str` | `"dev"` | Set to the git SHA/tag at image build; returned by `/api/health/live` (AC-18) and stamped on structlog + Langfuse so a trace maps to a release. Without it, "which build is live?" is unanswerable. |
| `COOKIE_SECURE` | `bool` | `False` | `Secure` is mandatory for a cross-site cookie and impossible on plain-HTTP localhost — so it cannot be a constant (AC-19). |
| `COOKIE_SAMESITE` | `Literal["lax","none"]` | `"lax"` | Vercel↔Render is cross-site; `lax` silently drops the anonymous session cookie in prod (AC-19). Default preserves current behaviour. |

Nothing else. Image tags, registry names, deploy hooks, Locust host and worker counts are
CI/platform environment, not application configuration, and are deliberately **not** added to
`Settings`.

---

## 7. Alembic migrations

**None.** F15 changes no model. AC-25's autogenerate guard enforces it.

---

## 8. Out of scope

- **F16 Telegram bot** — separate, optional feature.
- **Kubernetes, Terraform, autoscaling, blue/green, multi-region.** One Render instance, one Vercel
  project. Adding orchestration for a single-replica free-tier service is the exact
  over-engineering this project's philosophy forbids.
- **Running ingestion/indexing in production.** Indexing is a release step run from the ingestion
  image against the same Pinecone index (AC-31).
- **Prometheus/Grafana.** F13 already ships structlog + `request_logs` + `/internal/stats` +
  Langfuse. A second metrics stack for one instance earns nothing.
- **A new eval label.** F15 is Phase D; the fixed label sequence ends at `f17-memory-after`. F15's
  measurement artifact is the load-test report (AC-38), not an eval delta. The nightly smoke suite
  explicitly does not write a label (AC-34).
- **Multi-replica correctness.** The F9 cache matrix and the BM25 index are per-process; the
  existing `ponytail:` note in `Settings` already flags this. Single replica is the documented
  deployment.
