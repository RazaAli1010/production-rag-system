# F15 — Deployment, Docker, CI/CD & Load Testing · tasks.md

Ordered. Each task is ≤ ~1h and ends with something runnable. IDs map to the AC ids in
`requirements.md` §3.

## Status

| Task | State | Note |
|---|---|---|
| T1 · `/api/health/live` + `APP_VERSION` | **done** | tests green |
| T2 · configurable cookie attributes | **done** | tests green |
| T3 · `.dockerignore` + `.env.example` | **done** | `.env.example` existed; F15 section appended |
| T4 · `Dockerfile.ingestion` | **written** | build unverified — no Docker daemon on this machine |
| T5 · `make bm25` | **written** | needs Docker + a live corpus |
| T6 · `Dockerfile.serving` + entrypoint | **written** | build unverified; venv bug found and fixed by review |
| T7 · offline-weights check | **in CI** | `build-pr` job runs it with `--network none` |
| T8 · `api` service in compose | **written** | unverified — needs Docker |
| T9 · container SSE smoke | **blocked** | needs T8 |
| T10 · lint / types / model-drift | **done** | `ruff check app tests` and `mypy app` both green |
| T11 · migration round-trip job | **written** | job added; not executed locally |
| T12 · PR image build gate | **written** | needs a PR to fire |
| T13 · tag release path | **written** | needs secrets + a tag |
| T14 · provision and measure | **blocked** | needs Render/Neon/Upstash/Vercel accounts |
| T15 · public SSE + cookie check | **blocked** | needs T14, and a real browser |
| T16 · Locust file | **done** | question-mix self-check passes (`python loadtest/questions.py`) |
| T17 · the 50-user run | **blocked** | needs T14 — a run against a laptop measures the laptop |
| T18 · runbook | **done** | `docs/runbook.md` |
| T19 · architecture diagram | **done** | Mermaid in the README, not draw.io — see design §1 |
| T20 · README | **done** | benchmark numbers cross-checked against the committed reports |

Everything blocked is blocked on **deployment provisioning** or on Docker not running locally —
not on unfinished code.

**Build-order rationale.** The image comes before the pipeline that builds it (T3–T7 before
T10–T13), because a workflow that automates a broken `docker build` just automates it faster.
The three backend edits (T1–T2) come first because the image's healthcheck depends on the liveness
route existing. The load test comes last because it needs a real deployed URL to be worth running.

**Phase note.** F15 is Phase D, so there is **no eval gate** — the fixed label sequence ends at
`f17-memory-after`. F15's equivalent measurement artifact is the committed load-test report
(T16–T17). The nightly smoke suite writes no label.

---

## Phase 1 — Backend edits (T1–T2)

### T1 · `GET /api/health/live` + `APP_VERSION`
Add the three Settings keys (design §6). Append the liveness route to `app/api/health.py`. Bind
`APP_VERSION` into `configure_logging`'s context and the Langfuse tags alongside `APP_ENV`.
**Test:** `tests/api/test_health.py` — `/api/health/live` returns 200 with the configured version
**with Postgres, Redis and Pinecone all unreachable** (patch every probe to raise); `/api/health`
still 503s in the same scenario. That contrast IS the feature. (AC-16, AC-17, AC-18)

### T2 · Configurable session cookie attributes
`app/api/sessions.py`: `samesite=settings.COOKIE_SAMESITE`, `secure=settings.COOKIE_SECURE`.
**Test:** `tests/memory/` (or `tests/api/`) — defaults produce `SameSite=lax` and **no** `Secure`
attribute (byte-identical to today's `Set-Cookie`); `COOKIE_SAMESITE=none, COOKIE_SECURE=true`
produces `SameSite=none; Secure`. (AC-19)

---

## Phase 2 — Images (T3–T7)

### T3 · `.dockerignore` + `.env.example`
`.dockerignore`: `venv/`, `node_modules/`, `frontend/dist/`, `**/__pycache__/`, `.git/`,
`backend/app/data/raw/`, `backend/app/data/extracted/`, `docs/`. `.env.example`: every key in
`Settings` that has no default, plus the prod-only ones (`COOKIE_*`, `CORS_ALLOW_ORIGINS`,
`APP_ENV`, `ENABLE_*`), placeholder values only.
**Test:** `docker build` context size printed by the build is < 50 MB; `grep -f` finds no real
secret in `.env.example`; every `Settings` field without a default appears in it. (NFR-3)

### T4 · `Dockerfile.ingestion`
Per design §3. tesseract (eng+urd), ocrmypdf, libreoffice components, poppler, ghostscript.
**Test:** `docker run --rm campus-rag-ingest:local -c "import app.ingestion"` succeeds;
`tesseract --list-langs` includes `urd`; `libreoffice --version` prints. (AC-11)

### T5 · `make bm25` — produce the release artifact
Makefile targets `image-ingest` + `bm25` (design §9). Run against the real corpus once to produce
`docker/bm25.pkl` and `index_manifest.json`.
**Test:** `docker/bm25.pkl` exists and is non-empty; the manifest reports a **non-zero** chunk
count (the "indexed 0 docs exits 0" gotcha); `docker/*.pkl` is git-ignored but the file is present
locally. (AC-6)

### T6 · `Dockerfile.serving` + `entrypoint.sh`
Per design §2. CPU torch first, weights stage, non-root user, stdlib `HEALTHCHECK`,
`ARG APP_VERSION`, `exec uvicorn`.
**Test:** `make image` succeeds; `docker image inspect --format '{{.Size}}'` ≤ 2.5 GB;
`docker run --rm campus-rag-api:local python -c "import torch; print(torch.__version__)"` works and
`find / -name 'libcud*'` returns nothing; `id -u` inside the container is not 0. Delete
`docker/bm25.pkl` and confirm the build fails at the `COPY` with the artifact named.
(AC-1, AC-2, AC-4, AC-5, AC-7, AC-10)

### T7 · Verify the baked weights and the offline guarantee
**Test:** `docker run --network none campus-rag-api:local python -c "from sentence_transformers
import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2').predict([('a','b')])"`
succeeds with **no network at all**. This is the single check that proves AC-3 rather than assuming
it. (AC-3)

---

## Phase 3 — Local stack (T8–T9)

### T8 · `api` service in `docker-compose.yml`
Per design §7: build from repo root, `env_file`, overridden container-network URLs,
`depends_on: service_healthy`, port 8000. Add `make up`.
**Test:** from a clean `docker compose down -v`, `make up` reaches a 200 on
`/api/health/live` within 90 s and a 200 on `/api/health` once seeded; `make db-up` still starts
only postgres+redis. (AC-13, AC-14, AC-15)

### T9 · End-to-end smoke through the container
Ask one real question against the containerised API and consume the SSE stream to `done`.
**Test:** `curl -N -X POST localhost:8000/api/ask -H 'Accept: text/event-stream' -d '{"question":
"..."}'` emits `stage` → `token` → `citations` → `meta` → `done` in order, with at least one
citation. Confirms the baked bm25.pkl and weights work in situ, not just in `docker run`. (AC-8)

---

## Phase 4 — CI (T10–T13)

### T10 · `ci.yml` — `lint`, `types`, `model-drift`
Add repo-wide `ruff check app tests`; add mypy to `[dev]` with the committed `[tool.mypy]` config
(design §8.1) and a `types` job; add the generalised `model-drift` job and **delete** the duplicated
no-migration guard from the `api:` job.
**Test:** all three jobs green on `main` as-is (fix or explicitly override whatever mypy finds —
each override carries a one-line reason, no blanket `ignore_errors`); introducing a deliberate
unused import fails `lint`; adding a stray column to a model fails `model-drift`. (AC-22, AC-23,
AC-25, AC-27)

### T11 · `ci.yml` — `migrations` round-trip job
Postgres **and** Redis service containers → `upgrade head` → `downgrade base` → `upgrade head`.
**Test:** job green; temporarily removing a `downgrade()` body from a revision makes it red — which
is what proves the runbook's rollback step (AC-40) is real. (AC-24)

> Reminder from prior sessions: never run this locally alongside another pytest run against the
> shared dev DB — concurrent runs produce phantom failures. CI is isolated per job.

### T12 · `image.yml` — PR build gate
`paths:`-filtered PR trigger; build with a small committed **fixture** `bm25.pkl` (3 docs) so no PR
spends OpenAI/Pinecone quota; size gate.
**Test:** the job runs on a `backend/**` PR and is skipped on a docs-only PR; an artificially bloated
image fails the size step with the measured MB in the message. (AC-26, AC-4, NFR-4)

### T13 · `image.yml` — tag release path
Jobs `index` → `build` → `deploy` → `verify` (design §8.2). Secret-presence assertion first;
`curl -f` on the deploy hook; poll `/api/health/live` for `version == $GITHUB_SHA`.
**Test:** push `v0.1.0-rc1`; GHCR shows both the tag and the SHA tag; Render serves the new build;
`verify` passes. Then re-run with `RENDER_DEPLOY_HOOK_URL` unset and confirm it fails naming the
secret. (AC-28, AC-29, AC-30, AC-31, AC-32)

---

## Phase 5 — Deploy (T14–T15)

### T14 · Provision and measure
Render web service (Docker, GHCR image), Neon/Supabase Postgres, Upstash Redis, Vercel frontend.
Set every env var from `.env.example`, plus `APP_ENV=prod`, `COOKIE_SAMESITE=none`,
`COOKIE_SECURE=true`, `CORS_ALLOW_ORIGINS=["https://<app>.vercel.app"]`,
`VITE_API_BASE_URL=<render url>`. **Measure container RSS after rerank warmup** (NFR-1) and record
it.
**Test:** deployed `/api/health` is fully green (all five dependencies `ok`, redis not `skipped`);
`/api/health/live` reports the deployed SHA; RSS recorded in the runbook. If RSS is near the 512 MB
tier limit, decide and record: `ENABLE_RERANK=false` or a paid tier — the README then states which
configuration its benchmark numbers came from. (AC-18, AC-20, AC-21, NFR-1)

### T15 · Public SSE + cross-site cookie verification
From the deployed Vercel frontend in a real browser (not curl): ask a question anonymously, then
reload.
**Test:** the answer streams token-by-token over the public URL; DevTools shows the
`Set-Cookie` on `POST /api/sessions` carrying `SameSite=None; Secure` and the cookie being **sent**
on the following request; the session's history survives the reload. curl alone cannot verify this —
cross-site cookie policy is a browser behaviour. (AC-19, AC-20)

---

## Phase 6 — Load test (T16–T17)

### T16 · `loadtest/` — Locust file
`questions.py` (repeat/novel pools from `qa_dataset.jsonl`), `SingleTurnUser` (weight 7) +
`SessionUser` (weight 3, 3 sequential turns), half authed / half anonymous, SSE consumed to `done`
with TTFT as a custom metric, 409 `session_busy` tagged not failed (design §10).
**Test:** `locust --headless -u 2 -r 1 -t 30s --host http://localhost:8000` against the local stack
reports 0 failures and a non-zero cache hit rate on the repeat pool; killing the API mid-run
produces failures rather than false successes (proves AC-36's `catch_response` handling).
(AC-35, AC-36)

### T17 · The 50-user run + report
`make load LOAD_HOST=<render url>` at 50 users, 5 minutes, cache-warm. Pull the server-side window
query (design §10.4) or `/internal/stats`. Commit `docs/loadtest/<sha>-50u.md` with the raw CSVs,
the git SHA, the flag configuration, the instance tier, and the rate-limit configuration used.
**Test:** report committed; p95 < 4 s **or** a bottleneck analysis naming the dominant stage from
`retrieve_ms`/`rerank_ms`/`llm_ms` and the remediation considered. Either outcome satisfies the AC;
a missing analysis does not. (AC-37, AC-38, AC-39)

---

## Phase 7 — Docs (T18–T20)

### T18 · `docs/runbook.md`
Key rotation (OpenAI, Pinecone, **JWT — including that rotating `JWT_SECRET` invalidates every live
access token, while `refresh_tokens` remains the blacklist**), cache flush
(`python -m app.caching.run --flush`), corpus update → re-ingest → re-index → **rebuild bm25.pkl** →
cache invalidation → tag a release, migration rollback (`alembic downgrade -1`, and the AC-46 case
where redeploying an old image requires a downgrade first), reading a Langfuse trace from a
`request_id`, and the cold-start decision.
**Test:** a second person follows the cache-flush and rollback sections verbatim on the local stack
and both work with no improvisation. (AC-40, AC-41, AC-46)

### T19 · Architecture diagram
draw.io source + committed SVG: browser → Vercel → Render API → the pipeline seams (memory →
rewrite → cache → hybrid retrieve → rerank → refusal gate → compress → generate) → Pinecone /
Postgres / Redis / OpenAI, with Langfuse and `request_logs` on the observability edge.
**Test:** the SVG renders legibly in GitHub's markdown viewer at README width, in both light and
dark theme.

### T20 · `README.md`
Product statement, diagram, quickstart, the `baseline → F5 → F6 → F7 → F8 → F9` benchmark table
(each row linked to its `docs/eval_results/` report), the T17 load-test table, the JD keyword stack
table, and the honest status of the default-off flags.
**Test:** every benchmark number is greppable in a committed report under `docs/eval_results/`
(AC-43) — this is a real check, run it; the quickstart is executed verbatim on a clean clone into a
fresh directory and produces a streamed answer. Note the known-invalid `false_refusal_rate=1.0`
figures in the f7/f8 reports rather than quoting them. (AC-42, AC-43)

---

## Definition of done

- `docker compose up` brings up the full local stack from a clean clone; migrations apply
  automatically. **(AC-13)**
- CI is green with all service containers: nine module jobs + lint + types + migrations +
  model-drift + frontend + image build. **(AC-22–AC-27)**
- A `v*` tag builds, pushes to GHCR, deploys to Render, and verifies the live version — with a
  failing deploy hook or missing secret failing the workflow loudly. **(AC-28–AC-32)**
- Deployed `/api/health` is fully green and SSE is verified over the public URL **from a browser**,
  with the cross-site anonymous session cookie working. **(AC-15, AC-19, AC-20)**
- `docs/loadtest/<sha>-50u.md` is committed: p95 < 4 s at 50 users cache-warm, or a bottleneck
  analysis built from the per-stage `request_logs` columns. **(AC-38, AC-39)**
- `docs/runbook.md` and the README exist, and every README benchmark number matches a committed
  eval report. **(AC-40–AC-43)**
- No Alembic migration was added; `model-drift` proves it. **(AC-44)**
- Exactly three new Settings keys; no `os.environ` read introduced in `app/`. **(AC-45)**
