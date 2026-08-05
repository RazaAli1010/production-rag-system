# CampusRAG runbook

Operational procedures for the deployed stack: **Render** (API, Docker) · **Neon/Supabase**
(Postgres) · **Upstash** (Redis) · **Vercel** (frontend) · **Pinecone** (vectors) · **Langfuse**
(traces).

Every command below is copy-pasteable. Where a step is destructive it says so before the command,
not after.

---

## 0. Is it actually broken?

```bash
curl -s https://<api>/api/health/live          # process alive? → {"status":"live","version":"<sha>"}
curl -s https://<api>/api/health | jq          # per-dependency detail; 503 if any CORE dep is down
```

The two probes answer different questions and it matters which one is red:

| | `/api/health/live` | `/api/health` |
|---|---|---|
| Touches dependencies | no | postgres, redis, pinecone, bm25, openai key |
| Used by | container HEALTHCHECK, Render, the release workflow | you, monitoring, the UI banner |
| Red means | **the process is down** — restart | a dependency is degraded — read `dependencies` |

`redis: "skipped"` is not a fault: it means `REDIS_URL` is unset, which is a valid Postgres-only
deployment. `bm25: "missing"` **is** a fault — see §4.

`version` is the git SHA of the image serving traffic. If it is not the SHA you expect, the deploy
did not roll over (§6).

---

## 1. Cold start (Render free tier)

**The decision: accepted and documented, not mitigated.** The free instance spins down after ~15
minutes of inactivity; the next request pays the container start plus `alembic upgrade head` plus
uvicorn boot. The ~19s cross-encoder warmup is backgrounded in `_lifespan`, so it does not block
readiness, but the first ask after a cold start can still be slow enough that the F14 UI looks
hung — it is streaming nothing while the server boots.

If that becomes unacceptable, the two options, in order of laziness:

1. **Uptime pinger** — hit `/api/health/live` every 10 minutes from an external cron
   (UptimeRobot, or a `schedule:` GitHub Action). Cheap, keeps the instance warm, and the liveness
   route is the right target because it makes no billable call and touches no dependency.
2. **Paid tier** — no spin-down. Also the fix for §7's memory ceiling, so if you are paying for one
   you get the other.

Do not point a pinger at `/api/health` — it opens a Pinecone client and a DB connection every time.

---

## 2. Key rotation

### OpenAI / Pinecone

1. Create the new key in the provider console. **Do not delete the old one yet.**
2. Render → service → Environment → update `OPENAI_API_KEY` / `PINECONE_API_KEY` → save
   (this restarts the service).
3. Verify: `curl -s https://<api>/api/health | jq '.dependencies'` → `openai_key: "ok"`,
   `pinecone: "ok"`.
4. Ask one real question through the UI.
5. Only now revoke the old key.

Rotating the Pinecone key does not touch the index; rotating the OpenAI key does not invalidate
the F9 cache (entries are keyed on the query and the index manifest, not the credential).

### `JWT_SECRET` — read this before rotating

Rotating it **invalidates every access token in circulation instantly**. Every signed-in user gets
a 401 on their next call and must log in again. The refresh flow does not save them: a refresh
token is verified with the same secret.

`refresh_tokens` is the blacklist — a jti is valid iff a row exists with `revoked_at IS NULL` and
`expires_at` in the future. Rotation does not clear that table, so the rows are still there,
merely un-verifiable. There is nothing to clean up.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # new secret
# Render → Environment → JWT_SECRET → save (restarts the service)
```

Do it during a quiet window, and expect a burst of 401s in the logs immediately afterwards. That
burst is the rotation working.

### Cookie signing

The anonymous session cookie is signed with `JWT_SECRET` too, so rotation also orphans every
anonymous session. Those users silently get a new session on their next ask — no error, but their
history is gone.

---

## 3. Cache flush

The F9 cache has two tiers: Redis (exact match, TTL) and Postgres (`cache_entries`, semantic,
no TTL) plus an in-process embedding matrix per API worker.

```bash
# Flushes BOTH tiers.
cd backend && python -m app.caching.run --flush

# Surgical: remove one poisoned answer instead of the whole cache.
cd backend && python -m app.caching.run --delete-query "what is the attendance requirement"
```

Run it against the production `DATABASE_URL`/`REDIS_URL` (export them, or run it from a Render
shell). **The in-process matrix is rebuilt lazily and is per-worker**, so a running API keeps
serving from its resident copy until it restarts — flush, then restart the service if the stale
entries matter.

Flush after any corpus change (§4). A cached answer cites chunks that may no longer exist.

---

## 4. Corpus update → re-index → redeploy

This is the one procedure with real ordering constraints. The BM25 index is **baked into the
serving image**, so a corpus change is a release, not a config change.

```bash
# 1. Register the new/changed source
$EDITOR backend/app/data/sources.csv

# 2. Ingest + index from the ingestion image (tesseract/libreoffice live only there).
#    `make bm25` does both and copies the artifact into the build context.
make bm25

# 3. SANITY CHECK — do not skip. `app.indexing.run` exits 0 on an empty corpus
#    ("indexed 0 docs"), so the exit code proves nothing. Check the real vector counts:
cat backend/app/data/index_manifest.json          # namespaces.pu.vectors / .hec.vectors
#    ...and confirm against the Pinecone console. The manifest is written by the same run
#    that may have failed, so it is corroboration, not proof.

# 4. Invalidate the cache — cached answers cite the old chunk ids
cd backend && python -m app.caching.run --flush

# 5. Ship it. The tag pipeline rebuilds the index itself and bakes it into the image.
git tag v0.2.0 && git push origin v0.2.0
```

Replacing a document wholesale (rather than adding one) needs a namespace wipe first:

```bash
python -m app.indexing.run --strategy structure --namespace pu --wipe
```

**`--wipe` 404s on a namespace that does not exist yet** — that is not an error worth chasing on a
first run; it means there was nothing to wipe.

---

## 5. Migration rollback

Every revision has a `downgrade()`, and CI's `migrations` job proves the full round trip
(`upgrade head → downgrade base → upgrade head`) on every PR. That job exists precisely so this
section works at 3am.

```bash
cd backend
alembic current                 # where are we?
alembic history --verbose       # what is one step back?
alembic downgrade -1            # DESTRUCTIVE: a downgrade that drops a column loses that data
alembic current                 # confirm
```

Against production, export the production `DATABASE_URL` first, or run it from a Render shell.

**Take a snapshot before any downgrade that drops a column** (Neon: branch; Supabase: backup).
`downgrade()` is not an undo — it is a second forward migration that happens to reverse the
schema, and the data in a dropped column is gone.

---

## 6. Rolling back a release

Redeploying the previous image tag restores the previous release **without any database change**:

```bash
# Render → service → Settings → Docker image → ghcr.io/<owner>/campus-rag-api:<previous-tag>
# or re-run the deploy hook after repointing the tag.
curl -fsS -X POST "$RENDER_DEPLOY_HOOK_URL"
curl -s https://<api>/api/health/live       # `version` must be the OLD sha
```

**The one case where this is false:** the release you are rolling back from ran a forward
migration. The old image's code does not know about the new schema. Downgrade first (§5), then
redeploy the old image — in that order.

F15 itself adds no migration, and the `model-drift` CI job keeps it that way, so as of this
release a rollback is a pure image swap.

Rolling back a *pipeline* decision (rerank regressed, cache is poisoning answers) is not a release
rollback at all — the `ENABLE_*` flags are deployment config. Flip the env var on Render and
restart. That is the whole point of every enhancement being toggleable.

---

## 7. Memory pressure

Render's free tier is 512MB. Resident set after warmup is torch + the cross-encoder + the F9
in-process embedding matrix (`CACHE_MAX_ENTRIES=10_000` ⇒ ~61MB at capacity).

```bash
docker stats --no-stream campus-rag-api     # locally, after a first ask has warmed rerank
```

If it is near the tier ceiling, in order of preference:

1. `ENABLE_RERANK=false` — drops the cross-encoder entirely (costs the F6 retrieval gain).
2. Lower `CACHE_MAX_ENTRIES`.
3. Pay for the next tier.

`WEB_CONCURRENCY` stays at **1**. The cache matrix and the BM25 index are per-process, so a second
worker doubles resident memory *and* halves the cache hit rate.

---

## 8. Reading a trace

Every response carries a `request_id` (in the `meta` SSE frame, and the `X-Request-ID` header).
From it:

**Langfuse** — filter traces on `metadata.request_id = <id>`. One request produces up to three
traces (query rewrite, generation, and the memory summarizer when it fires); `request_id` is what
groups them, so filter, don't go looking for one nested tree. Each is stamped with the client's
`environment` (`APP_ENV`) and `release` (`APP_VERSION`), so you can scope to `prod` and to the exact
release, and asks in a session carry its `session_id`. The trace shows each LCEL step, the prompts,
and token usage.

Nothing in Langfuse? Check the boot log: `rag.langfuse_enabled` means the client was built (the
line carries the `base_url` it will post to), `rag.langfuse_disabled` means the keys weren't set,
`rag.langfuse_not_installed` means the image was built without `.[serving]`.

**Postgres** — the row that has every timing and cost:

```sql
SELECT ts, total_ms, embed_ms, retrieve_ms, rerank_ms, rewrite_ms, llm_ms,
       cache_hit, refused, degraded, memory_summarized,
       tokens_in, tokens_out, est_cost_usd, model, http_status, error_type,
       pipeline_flags
FROM request_logs
WHERE request_id = '<id>';
```

**Logs** — Render's log stream is JSON (structlog); every line carries `request_id`, `env` and
`version`.

The query text is **not** in `request_logs` — only `query_hash`. That is deliberate (telemetry
holds no raw queries). The raw text lives in `messages` for sessions, which is user-visible product
data:

```sql
SELECT role, content, created_at FROM messages
WHERE session_id = '<session_id>' ORDER BY created_at;
```

---

## 9. Password reset (added by PR #20, no schema change)

Three settings decide whether this feature works in production, and **none of them fail loudly**:

| Key | Dev default | Prod requirement |
|---|---|---|
| `FRONTEND_BASE_URL` | `http://localhost:5173` | the Vercel origin — it is the host of the reset link |
| `RESEND_API_KEY` | unset ⇒ the link is **logged, not emailed** | a real Resend key |
| `RESET_FROM_EMAIL` | `onboarding@resend.dev` | an address on a verified domain |

Left at defaults, a real user's reset email either never arrives or contains a `localhost` link,
and `/api/health` reports everything green throughout — none of these is a probed dependency. Set
them at provisioning time and verify by requesting one reset against the deployed app.

Until a domain is verified with Resend, it only delivers from `onboarding@resend.dev` **to the
account's own address**, so a test to any other mailbox silently goes nowhere.

Reset tokens are rate-limited by `RESET_MAX_PER_WINDOW` (per email, over
`LOGIN_LOCKOUT_WINDOW_MIN`) and expire after `RESET_TOKEN_TTL_MIN`. Rotating `JWT_SECRET` (§2)
invalidates outstanding reset links too.

---

## 10. Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/health` 503, `bm25: "missing"` | image built without a release index | §4, then redeploy |
| `/api/health` 503, `redis: "down: ..."` | Upstash blip | pipeline is fail-open — the API still answers. Check Upstash; no restart needed |
| Anonymous sessions never persist in prod | `COOKIE_SAMESITE`/`COOKIE_SECURE` not set for cross-site | set `none`/`true` on Render, confirm `Set-Cookie` in DevTools |
| Browser gets CORS errors | `CORS_ALLOW_ORIGINS` missing the Vercel origin | exact origin, never a wildcard (credentials are sent) |
| Deploy workflow green but old code serving | you deployed without the verify step | `/api/health/live` `version` is the source of truth |
| First request after idle hangs ~40s | free-tier cold start | §1 |
| Everything refuses | index empty or wrong namespace | check `index_manifest.json` and Pinecone counts (§4 step 3) |
| Burst of 401s right after a config change | `JWT_SECRET` was rotated | expected; users re-login (§2) |
| Password-reset emails link to `localhost:5173` | `FRONTEND_BASE_URL` left at its dev default | set it to the Vercel origin (§9) |
| No reset email arrives; the link is in the logs | `RESEND_API_KEY` unset — the documented dev fallback | set the key in prod (§9) |
