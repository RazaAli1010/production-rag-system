# F15 — Deployment, Docker, CI/CD & Load Testing · design.md

Companion to `requirements.md`. Every AC id below refers to that file.

---

## 1. Module layout

```
campus-rag/
├── README.md                        # NEW — portfolio centerpiece (AC-42)
├── .env.example                     # NEW — every key, placeholder values only (NFR-3)
├── Makefile                         # EXTENDED — image/compose/loadtest targets
├── docker/
│   ├── Dockerfile.serving           # NEW — multi-stage, CPU torch, HF weights baked
│   ├── Dockerfile.ingestion         # NEW — serving deps + tesseract/libreoffice/ocrmypdf
│   ├── entrypoint.sh                # NEW — alembic upgrade head && exec uvicorn
│   ├── docker-compose.yml           # EXTENDED — adds the `api` service
│   └── .dockerignore                # NEW — keeps venv/node_modules/raw corpus out of context
├── .github/workflows/
│   ├── ci.yml                       # EXTENDED — lint, typecheck, migration round-trip, model drift
│   ├── frontend.yml                 # UNCHANGED
│   ├── image.yml                    # NEW — PR: build-only. Tag: build+push GHCR+deploy+verify
│   └── nightly.yml                  # NEW — F4 retrieval smoke (stretch, secret-gated)
├── loadtest/
│   ├── locustfile.py                # NEW — SSE-aware user classes
│   ├── questions.py                 # NEW — repeat/novel pools drawn from the eval dataset
│   └── README.md                    # NEW — how to run it, how to read the output
├── docs/
│   ├── runbook.md                   # NEW (AC-40, AC-41)
│   │   # architecture diagram: a Mermaid block inside README.md, NOT a draw.io + SVG pair.
│   │   # GitHub renders mermaid natively, so the diagram stays diffable text and the repo needs
│   │   # neither a binary asset nor an export step to keep it in sync with the code.
│   └── loadtest/<sha>-50u.md        # NEW per run + raw Locust CSVs (AC-38)
└── backend/
    ├── pyproject.toml               # EXTENDED — [dev] += mypy; new [load] extra = locust
    └── app/
        ├── core/settings.py         # +3 keys (§6)
        ├── api/health.py            # + GET /api/health/live (§4)
        └── api/sessions.py          # cookie attrs read from Settings (§5)
```

Backend code touched: **three files, ~15 lines total.** That is the whole point — F11/F13/F14 froze
the surface; F15 packages it.

---

## 2. Serving image

### 2.1 Why multi-stage, concretely

`pip install -e ".[dev]"` in one stage pulls compilers and a ~1 GB pip cache. The runtime stage
copies only the installed site-packages and the app.

Three stages: `builder` (venv + deps) → `weights` (download the cross-encoder) → `runtime`. See
`docker/Dockerfile.serving` for the committed file.

Notes that matter:

- **A venv, not `pip install --prefix=/install`.** The first draft of this design used `--prefix`
  and it was wrong: pip does not put a `--prefix` target on `sys.path`, so the project install
  would not see torch as satisfied and would pull the **CUDA wheel from PyPI** — silently blowing
  the size budget — while the runtime stage's `COPY --from=builder /install /usr/local` would ship
  an image with no torch at all. `python -m venv /opt/venv` + `ENV PATH=/opt/venv/bin:$PATH` fixes
  both: one self-contained directory to copy, and the second `pip install` resolves against the
  first.
- **The torch pin is read out of `pyproject.toml`** (`grep -oE '"torch==[^"]+"'`) rather than
  repeated in the Dockerfile. A second hardcoded pin drifts silently, and the symptom — a 2.5GB
  image or a resolver conflict — only appears at build time.
- **`HEALTHCHECK` hits `/api/health/live`, not `/api/health`** (AC-9/AC-16). See §4.
- **No curl installed** — the healthcheck uses stdlib `urllib`, saving a package and an apt layer.
- **`HF_HUB_OFFLINE=1` in runtime only.** Setting it in the weights stage would prevent the
  download it exists to perform.
- **`--start-period=40s`** covers boot + `alembic upgrade head` before failures count.
- The image contains **no `[dev]` extras**: ruff/pytest/mypy/locust never ship.

### 2.2 Where `bm25.pkl` comes from (AC-6/AC-7)

`backend/app/data/*.pkl` is git-ignored, so the artifact must be produced, not committed.

```
release tag
   └─ job `index` : run the INGESTION image
        python -m app.ingestion.run --all
        python -m app.indexing.run --strategy structure --namespace all
        → uploads backend/app/data/bm25.pkl as a workflow artifact
   └─ job `image`  (needs: index)
        downloads the artifact → docker/bm25.pkl → docker build
```

`python -m app.indexing.run` writes both `bm25.pkl` and `index_manifest.json`. Only `bm25.pkl` is
baked into the image — nothing at serving time reads the manifest, and `APP_VERSION` already
answers "which release is this?", so copying it in would be provenance nobody queries. The
`index` job still asserts against the manifest before uploading. Rebuilding the index and the image
in the same job is what makes "release-matched" true — a serving image whose BM25 vocabulary
predates the Pinecone namespace it queries produces silently worse hybrid retrieval, and nothing in
`/api/health` would catch it.

> **Known gotcha (memory, `indexing.run`):** "indexed 0 docs" exits **0** on an empty corpus. The
> index job therefore asserts a non-zero chunk count from the manifest before uploading, rather
> than trusting the exit code.

### 2.3 Entrypoint

```sh
#!/bin/sh
# docker/entrypoint.sh — AC-8
set -e
alembic upgrade head          # `set -e` ⇒ a failed migration exits non-zero, uvicorn never starts
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WEB_CONCURRENCY:-1}"
```

`exec` so uvicorn is PID 1 and receives SIGTERM directly — otherwise Render's shutdown grace period
kills the container mid-stream instead of letting `_lifespan` close the Redis pool.

`WEB_CONCURRENCY` defaults to **1**, deliberately: the F9 cache matrix and the BM25 index are
per-process (`ponytail:` note in `Settings`), so a second worker doubles resident memory and halves
the cache hit rate. Multi-worker is a paid-tier decision, documented in the runbook.

---

## 3. Ingestion image

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-urd \
      ocrmypdf libreoffice-writer libreoffice-impress libreoffice-calc \
      poppler-utils ghostscript build-essential \
    && rm -rf /var/lib/apt/lists/*
...same CPU-torch + project install as serving...
ENTRYPOINT ["python", "-m"]
```

- `tesseract-ocr-urd` because `OCR_LANGUAGES="eng+urd"` (Settings). Omitting it makes every scanned
  Urdu page fail at runtime with a language-data error.
- `libreoffice-*` component packages, not the `libreoffice` metapackage — the metapackage drags in
  Base/Draw and ~400 MB for formats `LIBREOFFICE_BIN` is never asked to convert.
- No size budget: this image is never deployed (AC-12).

---

## 4. `GET /api/health/live`

```python
# app/api/health.py — appended
@router.get("/health/live", summary="Liveness (no dependencies)",
            description="200 whenever the process is up. Used by the container HEALTHCHECK and "
                        "the platform probe; /api/health is the deep readiness probe.")
async def live() -> dict[str, str]:
    return {"status": "live", "version": settings.APP_VERSION}
```

**Route-order note:** `/api/health/live` must be registered *after* `/api/health` in the same
router — FastAPI matches the literal path, so no conflict arises, but keeping the deep probe first
preserves the OpenAPI ordering the F14 client was generated against.

**Why this is not over-engineering.** `/api/health` returns 503 when *any* core dependency is down,
including Redis. Redis is fail-open in the pipeline (`redis_hot` short-circuits to a miss on
timeout/outage — proven by `tests/cache/test_redis_hot.py`). Wiring a restart-on-failure platform
probe to it means an Upstash blip restarts an API that can still answer every question, and the
restart itself costs a ~19 s rerank warmup. The two probes answer different questions:

| Probe | Question | Consumer | On failure |
|---|---|---|---|
| `/api/health/live` | is the process alive? | Docker HEALTHCHECK, Render | restart |
| `/api/health` | can it serve well? | runbook, monitoring, F14 degradation banner | page a human |

---

## 5. Cross-site cookie (AC-19)

```python
# app/api/sessions.py — the only change
response.set_cookie(
    cookies.COOKIE_NAME, cookies.sign(s.id, settings=settings),
    httponly=True,
    samesite=settings.COOKIE_SAMESITE,   # was: "lax"
    secure=settings.COOKIE_SECURE,       # new — required whenever samesite="none"
    max_age=settings.MEMORY_ANON_TTL_DAYS * 86_400,
)
```

Prod env: `COOKIE_SAMESITE=none`, `COOKIE_SECURE=true`, `CORS_ALLOW_ORIGINS=["https://<app>.vercel.app"]`.
Local/CI defaults are unchanged (`lax`/`false`), so nothing about the current test suite moves.

**Rejected alternative — Vercel rewrites** (`/api/*` → Render, making everything same-origin and
the cookie change unnecessary). It puts Vercel's edge proxy in the path of an **SSE** stream, which
adds a gateway timeout ceiling and a buffering variable to the one endpoint whose whole value is
incremental delivery. Two Settings keys are the smaller and more predictable diff.

> **Cross-check:** browsers reject `SameSite=None` without `Secure`, and reject `Secure` cookies on
> plain `http://`. Hence two keys, not one — a single `COOKIE_CROSS_SITE` bool would be smaller but
> makes local HTTPS-less testing of the prod configuration impossible.

---

## 6. Settings additions

```python
# --- Deployment (F15) ---
# Stamped at image build (`--build-arg APP_VERSION=$GITHUB_SHA`). Returned by /api/health/live and
# attached to every structlog line, so a Langfuse trace maps to an exact release (AC-18).
APP_VERSION: str = "dev"
# The Vercel frontend and the Render API are cross-site, so the anonymous session cookie needs
# SameSite=None; Secure. Defaults keep local/dev behaviour byte-identical (AC-19).
COOKIE_SECURE: bool = False
COOKIE_SAMESITE: Literal["lax", "none"] = "lax"
```

`APP_VERSION` is added to the `configure_logging` bound context in `app/observability/logging.py`
alongside the existing `APP_ENV`, and to the Langfuse handler's tags — one line each, satisfying
"every metric mentioned must actually be logged" for the release dimension.

---

## 7. `docker-compose.yml` (extended)

```yaml
services:
  postgres: { …unchanged… }
  redis:    { …unchanged… }

  api:
    build:
      context: ..                       # repo root — the Dockerfile COPYs backend/
      dockerfile: docker/Dockerfile.serving
      args: { APP_VERSION: dev }
    env_file: [ ../backend/.env ]
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:postgres@postgres:5432/campus_rag
      REDIS_URL: redis://redis:6379/0
    ports: [ "8000:8000" ]
    depends_on:
      postgres: { condition: service_healthy }   # AC-14
      redis:    { condition: service_healthy }
```

- `env_file` supplies the secrets (`OPENAI_API_KEY`, `PINECONE_*`, `JWT_SECRET`, `ADMIN_*`); the
  two `environment` entries **override** the host-oriented URLs from `.env` with the compose network
  names. Env precedence is what makes one `.env` serve both bare-metal and containerised runs.
- Local `docker compose build` needs a `docker/bm25.pkl`; `make bm25` (§9) produces it from the
  ingestion image, and the failure message from AC-7 tells you to run it.
- `make db-up` keeps naming only `postgres redis`, so AC-15 holds — nothing about today's workflow
  changes for a contributor who never touches the api service.

---

## 8. CI

### 8.1 `ci.yml` — jobs added (existing nine untouched, AC-27)

| Job | Does | AC |
|---|---|---|
| `lint` | `ruff check app tests` over the whole backend | AC-22 |
| `types` | `mypy app` in basic mode | AC-23 |
| `migrations` | Postgres **+ Redis** services → `upgrade head` → `downgrade base` → `upgrade head` | AC-24 |
| `model-drift` | `alembic revision --autogenerate` → fail if it contains any `op.*` schema call | AC-25 |

`model-drift` generalises the existing F11 `No-migration guard`, which is currently duplicated
inside the `api:` job. That duplicate is **deleted** as part of T10 — one guard, repo-wide.

mypy config (committed, AC-23):

```toml
[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true    # ragas/langfuse/sentence-transformers ship no usable stubs
check_untyped_defs = true
# "basic mode": no --strict. disallow_untyped_defs would demand annotating the whole
# pre-existing codebase, which is a refactor, not a deployment feature.
```

Any module needing an escape hatch gets its own `[[tool.mypy.overrides]]` block with a one-line
reason. A blanket `ignore_errors` is forbidden — it would make the gate decorative.

### 8.2 `image.yml` — build on PR, release on tag

```yaml
on:
  pull_request:
    paths: ["backend/**", "docker/**", ".github/workflows/image.yml"]   # AC-26, NFR-4
  push:
    tags: ["v*"]                                                        # AC-28
```

| Job | PR | Tag |
|---|---|---|
| `index` | skipped (a synthetic 3-doc `bm25.pkl` fixture is used instead — a PR must not spend OpenAI/Pinecone quota) | runs the ingestion image, asserts chunk count > 0, uploads `bm25.pkl` + manifest |
| `build` | build only, `--load`, assert size ≤ 2.5 GB | build + push to `ghcr.io/<owner>/campus-rag-api:{tag,sha}` |
| `deploy` | — | `curl -fsS "$RENDER_DEPLOY_HOOK_URL"` (`-f` ⇒ non-2xx fails the job, AC-29) |
| `verify` | — | poll `GET $PUBLIC_URL/api/health/live` until `version == $GITHUB_SHA`, 10 min timeout (AC-30) |

Size gate:

```sh
BYTES=$(docker image inspect "$IMAGE" --format '{{.Size}}')
[ "$BYTES" -le 2684354560 ] || { echo "image $((BYTES/1024/1024))MB > 2560MB budget (AC-4)" >&2; exit 1; }
```

Secret presence is asserted in the first step of `deploy` so a missing secret fails loudly rather
than deploying nothing and reporting green (AC-32).

### 8.3 `nightly.yml` (stretch, AC-33/34)

`schedule: cron` + `workflow_dispatch`.

**No 10-question cap, contrary to the brief.** `app.evals.run` has no `--limit` flag, and adding
one to F4's CLI to save a few embedding calls is not worth it: the retrieval suite runs no RAGAS
judge, so the full 75-record set already costs cents. A fixed 10-question subset would also only
ever measure the ten questions someone happened to pick. Flags are passed explicitly
(`--flags hybrid=on,rerank=on`) because the Settings defaults are all-off, and the job would
otherwise silently measure the F3 baseline instead of the deployed posture.

First step:

```yaml
- id: gate
  run: echo "ok=${{ secrets.OPENAI_API_KEY != '' && secrets.PINECONE_API_KEY != '' }}" >> $GITHUB_OUTPUT
```

Every later step is `if: steps.gate.outputs.ok == 'true'` — a fork without secrets gets a green
skip, not a red X. `EVAL_RESULTS_DIR` (a Settings key) is redirected to a temp dir, so **nothing is written into
`docs/eval_results/`** (AC-34). The floor check parses the generated report's metric table
(`| hit@5 | overall | 0.9683 |`) against a committed
`docs/eval_results/nightly_floor.json`; regression opens an issue via `gh issue create`.

> **Known gotchas (memory):** eval runs need `OPENAI_API_KEY` exported in the environment and
> `PYTHONIOENCODING`/`PYTHONUTF8` set for the code-switched records; `--compare` reads the previous
> label from the **database**, so the nightly job — which has no seeded eval history — compares
> against a committed JSON baseline file instead of `--compare`.

---

## 9. Makefile additions

```make
image:        ## build the serving image locally
	docker build -f docker/Dockerfile.serving --build-arg APP_VERSION=$$(git rev-parse --short HEAD) -t campus-rag-api:local ..
image-ingest:
	docker build -f docker/Dockerfile.ingestion -t campus-rag-ingest:local ..
bm25: image-ingest   ## regenerate docker/bm25.pkl for a local image build
	docker run --rm --env-file backend/.env -v $$PWD/backend/app/data:/app/app/data campus-rag-ingest:local app.indexing.run --strategy structure --namespace all
	cp backend/app/data/bm25.pkl docker/bm25.pkl
up:
	docker compose -f docker/docker-compose.yml up --build
load:         ## LOAD_HOST=https://… make load
	locust -f loadtest/locustfile.py --headless -u 50 -r 5 -t 5m --host $$LOAD_HOST --csv docs/loadtest/raw/$$(git rev-parse --short HEAD)
```

---

## 10. Load test

### 10.1 Shape

```python
# loadtest/locustfile.py
class SingleTurnUser(HttpUser):     # weight=7 — the 70% who ask one question
    weight = 7
class SessionUser(HttpUser):        # weight=3 — AC-35's ~30% multi-turn cohort
    weight = 3
```

- **Question mix (AC-35):** `questions.py` loads answerable records from
  `backend/app/data/evals/qa_dataset.jsonl` and splits them into a 12-question **repeat pool**
  (drawn 80% of the time — these are what the cache is supposed to serve) and a **novel pool**
  (20%, each question suffixed with a nonce so it can never hit the cache). Reusing the eval
  dataset means the load test asks questions the corpus can actually answer; random strings would
  measure the refusal path at 50 users, which is not the thing under test.
- **Auth mix:** ~half the users log in once at `on_start` via `POST /api/auth/token` and carry the
  bearer token; the rest stay anonymous. This exercises both rate-limit tiers
  (`RATE_LIMIT_ANON_PER_MIN=5` vs `RATE_LIMIT_STUDENT_PER_MIN=20`).
- **`SessionUser`** does `POST /api/sessions` → three sequential asks reusing `session_id`, with a
  think-time between turns. Sequential is mandatory, not stylistic: F17 holds a per-session
  `asyncio.Lock` and a concurrent second ask on the same session returns **409 `session_busy`**.
  The client treats 409 as a *tagged expected outcome*, not a failure — but a 409 rate above ~1%
  means the think-time is too short and the run is invalid.

### 10.2 SSE consumption (AC-36)

```python
with self.client.post("/api/ask", json=payload, headers={"Accept": "text/event-stream"},
                      stream=True, catch_response=True, name="POST /api/ask") as r:
    saw_done = False
    for raw in r.iter_lines():          # frames are `event: X\ndata: {...}\n\n`
        ...
        if event == "done":  saw_done = True; break
        if event == "error": r.failure(f"stream error: {data}"); break
    r.failure("stream ended without done") if not saw_done else r.success()
```

Locust's default `catch_response=False` would mark a streaming request successful at response
*headers* — i.e. it would report the p95 of "server accepted the connection", which for SSE is
near-zero and completely meaningless. Timing is stopped at the `done` frame; TTFT (first `token`
frame) is recorded as a separate custom metric, because for a streaming UI it is the number the
user actually perceives.

### 10.3 Rate limiting during the run

50 users against `RATE_LIMIT_ANON_PER_MIN=5` produces a 429 storm that measures the limiter, not
the pipeline. The run configuration therefore either raises the anon tier for the duration or runs
predominantly authenticated, and **the report states which** — a p95 achieved by being rejected is
not a p95. 429s are tagged and reported separately from errors.

### 10.4 Server-side numbers (AC-37)

Locust gives client latency, RPS, and error rate. Cache hit rate and per-stage attribution come
from the server, over the run's time window:

```sql
SELECT count(*), avg(cache_hit::int),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms),
       avg(retrieve_ms), avg(rerank_ms), avg(llm_ms)
FROM request_logs WHERE ts BETWEEN :start AND :end;
```

Every column above already exists (F13, `app/db/models/ops.py`) — **F15 adds no logging.** The
admin `GET /internal/stats?window=…` returns the same aggregation and is the no-SQL path.

If AC-39's p95 < 4 s is missed, the bottleneck analysis is exactly `avg(retrieve_ms)` vs
`avg(rerank_ms)` vs `avg(llm_ms)` from that query — which is why the per-stage columns, not just
`total_ms`, are what the report table is built from.

---

## 11. Data flow

```
CONTRIBUTOR                    make up
   └─ docker compose ─┬─ postgres (healthy) ──┐
                      ├─ redis    (healthy) ──┤
                      └─ api ← depends_on ────┘
                            entrypoint.sh: alembic upgrade head → uvicorn
                            HEALTHCHECK → /api/health/live

PULL REQUEST                   ci.yml (9 module jobs + lint + types + migrations + model-drift)
                               frontend.yml (paths-filtered)
                               image.yml   (paths-filtered, build-only, size gate)

TAG v*                         image.yml
   index ─ ingestion image → bm25.pkl + manifest ─ artifact ─┐
   build ─ docker build (APP_VERSION=$SHA) ← bm25.pkl ───────┘ → GHCR
   deploy ─ POST Render deploy hook
   verify ─ poll /api/health/live until version == $SHA
                               Vercel auto-deploys frontend on the same push

OPERATOR                       make load LOAD_HOST=https://…
   locust (50u, 80/20, 30% 3-turn) ──▶ POST /api/ask (SSE to `done`)
                                          └─▶ request_logs ──▶ /internal/stats
   report ← Locust CSVs + the SQL window query → docs/loadtest/<sha>-50u.md → README
```

---

## 12. Error handling

| Failure | Behaviour | Where |
|---|---|---|
| `bm25.pkl` missing from build context | build fails at `COPY`, naming the artifact | AC-7 |
| Indexing produced 0 chunks | `index` job asserts manifest chunk count > 0 and fails | §2.2 |
| `alembic upgrade head` fails at boot | `set -e` → container exits non-zero, no traffic served | AC-8 |
| Image > 2.5 GB | size gate fails the job | AC-4 |
| Render deploy hook non-2xx | `curl -f` fails the job | AC-29 |
| Deployed version never matches | `verify` times out at 10 min and fails | AC-30 |
| Missing CI secret | asserted in step 1, fails with the secret's name | AC-32 |
| Nightly without eval secrets | gated skip, green | AC-33 |
| Redis down in prod | pipeline degrades (fail-open), `/api/health` 503s, **`/api/health/live` stays 200 ⇒ no restart loop** | §4 |
| Cold start after spin-down | first request pays the wake; documented, mitigated or accepted | AC-41 |
| SSE stream ends without `done` | Locust marks the request failed | AC-36 |
| 409 `session_busy` under load | tagged separately; >1% invalidates the run | §10.1 |

---

## 13. How this honours the Shared Context

- **SSE contract** — the Locust client is a *consumer* of `stage*` → `token*` → `citations` →
  `meta` → `done`|`error` and asserts the terminal frame. It adds no event type and tolerates
  unknown `stage` ids (`stage` is a bare `str`, per the F14 finding).
- **F3 retriever seam** — untouched. F15 never imports `app.rag`; it only builds and calls the API.
- **Pipeline order** — unchanged. No new seam, no new stage, no new flag.
- **Toggleability** — `ENABLE_*` flags stay a deployment decision made through env vars on Render,
  which is exactly the mechanism CLAUDE.md prescribes (there is no per-request `flags_override`).
  Rollback of an enhancement in prod is an env-var change plus a restart; rollback of a *release*
  is redeploying the previous GHCR tag (AC-46).
- **Central Settings** — the three new keys live in the one `Settings` class; CI/platform config
  (registry, deploy hook, Locust host) stays out of it (AC-45).
- **Migrations** — none; the `model-drift` job proves it (AC-44).
- **Metrics** — every number the README quotes comes from Locust CSVs or from `request_logs`
  columns that F13 already writes. F15 adds no metric and therefore adds no logging.
- **Eval labels** — the fixed sequence is untouched; the nightly smoke deliberately writes no
  label. F15's artifact is `docs/loadtest/`, not `docs/eval_results/`.
