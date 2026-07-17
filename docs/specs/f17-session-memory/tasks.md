# F17 — Session Memory & Chat Experience · tasks.md

**Module:** `backend/app/memory/` · **Phase:** C · **Depends on:** F12, F3, F7, F10 · **Flag:**
`ENABLE_MEMORY` · **Eval gate:** `f17-memory-after` vs `f9-cache-after`

Each task is ≈ ≤ 1 hour and lands green. Order is bottom-up: pure-CPU token/window logic first (the
rule the whole feature rests on), then the summariser, then the async service + routers in isolation,
then the splice into `_pipeline_events` and the `/api/ask` route, then toggling, then the gate.
`baseline.py` changes only after `window.py` passes its own tests.

**T15 IS the feature.** Per CLAUDE.md, F17 is not done when the code works; it is done when
`docs/eval_results/f17-memory-after-vs-f9-cache-after.md` is committed.

**No Alembic task exists on purpose** — F12 already migrated `sessions`/`messages` (design §2/§8). T14
*asserts* autogenerate stays empty; adding a migration is a design regression.

---

### T1 — Settings block + MemoryContext fields + test scaffold
Add the `# --- Session memory (F17) ---` block from design §7 to `app/core/settings.py` (all `MEMORY_*`
+ `ENABLE_MEMORY`). Add `window_pairs: int = 0` and `effective_tokens: int = 0` to
`core.contracts.MemoryContext` (additive, design §5). Create `backend/tests/memory/conftest.py`
mirroring `tests/cache/conftest.py` (own engine/session, `lru_cache` reset, autouse env stubs, plus
`sessions`/`messages` in the `TRUNCATE` teardown).

**Test:** `tests/memory/test_settings_schemas.py` — defaults exactly `ENABLE_MEMORY is False`,
`MEMORY_TOKEN_BUDGET == 50_000`, `MEMORY_WINDOW_PAIRS == 5`, `MEMORY_KEEP_LAST_PAIRS == 2`,
`MEMORY_SUMMARIZE_EVERY_PAIRS == 3`, `MEMORY_SUMMARY_MAX_TOKENS == 600`; `MemoryContext()` defaults
`window_pairs == 0`, `effective_tokens == 0` (AC-34, contract additive).

---

### T2 — `app/memory/tokens.py`
`count(text: str) -> int` via `tiktoken.get_encoding("cl100k_base")` (module-level encoder, same
pattern as `baseline._ENC`). Pure CPU, inline, no settings arg (AC-13/32).

**Test:** `tests/memory/test_tokens.py` — `count("")==0`, exactness on a known string, and a
round-trip assertion that `count` equals `len(enc.encode(text))` (the "tiktoken is exact" edge case).

---

### T3 — `app/memory/window.py`
`assemble(session, recent, pending, settings) -> MemoryContext` + `_last_whole_pairs(recent, n)` per
design §3.1. Pure function, no I/O, no LLM. Keeps pairs whole (user+assistant together), handles a lone
huge message without breaking the pair math (AC-21), and sets `window_pairs`/`effective_tokens`/
`summarized` correctly for all three states.

**Test:** `tests/memory/test_window.py` — feed synthetic `Session`/`Message` objects:
`≤5` pairs → all verbatim, no summary, `window_pairs==5`, `summarized False` (AC-18); `>5` under budget
→ last 5 + summary, `window_pairs==5` (AC-19); `total_tokens >= 50_000` → last 2 + summary,
`window_pairs==2`, `summarized True`, `effective_tokens < 50_000` (AC-20); a 4k-token lone message
still yields whole pairs (AC-21).

---

### T4 — `app/memory/summarizer.py`
`async extend_summary(old_summary, pending, settings)` — one `ChatOpenAI` `ainvoke` call, temp
`MEMORY_SUMMARY_TEMPERATURE`, `max_tokens=MEMORY_SUMMARY_MAX_TOKENS`, model `MEMORY_SUMMARY_MODEL`.
Prompt per design §3.2 (extend, don't re-summarize; facts/answers/citations/threads; refusals excluded
from "facts", AC-24/28). Logs token+cost via `estimate_cost` like every other OpenAI call. Raises on
failure (caller handles).

**Test:** `tests/memory/test_summarizer.py` — monkeypatch the LLM: assert the prompt contains only
`old_summary + pending` and never the full transcript (AC-24); a raised LLM error propagates (caught by
the caller in T6).

---

### T5 — `app/memory/service.py` (DB ops + lock + write-behind)
`create_session`, `list_sessions`, `get_owned` (404 on foreign, AC-6), `get_messages` (full transcript,
AC-4), `archive` (AC-5), `persist_user` (writes user msg first, auto-title if NULL truncated to
`MEMORY_SESSION_TITLE_MAX_CHARS`, `total_tokens +=`, AC-2/10/13), `schedule_persist_assistant`
(`create_task` write-behind with strong-ref set + done-callback, AC-11), `load_memory` (one indexed
round trip: session row + last-`window` messages via `ORDER BY created_at DESC LIMIT`, returns
`(MemoryContext, pending_or_None)` deciding the summariser call from `summarized_upto_message_id`,
AC-22/23/25), and the per-session `asyncio.Lock` registry (`acquire_or_409`).

**Test:** `tests/memory/test_service.py` (live Postgres) — user write precedes assistant write by
`created_at` (AC-10); `total_tokens` == sum of message `token_count`s (AC-13); `get_owned` on a foreign
session raises 404 (AC-6); `list_sessions` is `last_active_at DESC` non-archived (AC-3/5);
`load_memory` issues exactly one query (assert via SQL echo/counter) and returns only the window
(AC-22); write-behind task persists after `done` and holds its ref.

---

### T6 — `app/memory/cookies.py` (anon session signing)
`sign(session_id) -> str` / `verify(cookie) -> uuid | None` reusing `core.security`'s pyjwt surface with
`typ="anon_session"` and `JWT_SECRET` (no new dependency, design §4). Bad/forged/expired cookie → `None`
(AC-8).

**Test:** `tests/memory/test_cookies.py` — round-trip; a tampered token, a wrong-secret token, and a
wrong-`typ` access token all verify to `None`.

---

### T7 — `app/memory/stages.py` (single stage emitter)
`emit(queue, stage, status, ms=None)` fire-and-forget over a bounded `asyncio.Queue` (drop on full,
never block, AC-29). Provide the wrapper the ask route uses to fold `summarizing_memory` in front of the
existing F3 `stage` sequence. Reuses `rag.events.stage_event` for the payload shape (no new event type).

**Test:** `tests/memory/test_stages.py` — a full queue drops the event without raising; emitted events
carry `ms` on `done` and use the existing `StageEvent` shape.

---

### T8 — `app/api/sessions.py` (REST router)
`POST /api/sessions` (authed → user-bound; anon → signed cookie, AC-1), `GET /api/sessions` (authed,
AC-3), `GET /api/sessions/{id}/messages` (owner, full transcript, AC-4), `DELETE /api/sessions/{id}`
(archive, AC-5). Ownership via `service.get_owned` → 404 (AC-6). Register in `main.py`.

**Test:** `tests/memory/test_sessions_api.py` (httpx ASGI) — create/list/transcript/archive happy path;
foreign session → 404; anonymous create sets an `httpOnly` signed cookie; auto-title == first question
truncated to 60 chars (AC-2).

---

### T9 — `baseline.py` seam: surface session_id + memory_summarized
Stop hardcoding `session_id=None` / `memory_summarized=False` at the two `AnswerResponse` sites; take
them from a new `session_id: str | None = None` kwarg on `astream`/`answer`/`_pipeline_events` and from
`memory.summarized`. Change `langfuse_handler(session_id=None)` → `session_id=session_id`. Emit
`stage summarizing_memory` `skipped` when no summariser runs (the ask route emits `started/done` when it
does). No retrieval/rerank/compress/cache seam changes (design §5).

**Test:** extend `tests/rag/` — a memory-on `answer()` call surfaces `session_id` and
`memory_summarized` on the response; a memory-off call is unchanged (regression, feeds AC-33).

---

### T10 — `app/api/ask.py` (session-aware SSE route)
`POST /api/ask {question, session_id?}` → `StreamingResponse`. Resolves principal (F10 optional) + anon
cookie; memory-off or no `session_id` → delegate straight to stateless `astream` (AC-33). Else: acquire
per-session lock (held → `409 session_busy`, AC-31), `persist_user` first (AC-10), `load_memory` +
optional `summarizer.extend_summary` with `stage summarizing_memory started/done` (AC-23/25/26) wrapped
in the `memory.summarize_failed` fallback (AC-27), then `astream(..., memory=mem, session_id=...)`. On
clean `done`, `schedule_persist_assistant`; on `request.is_disconnected()` before `done`, skip the
assistant write (AC-11/12). Register in `main.py`.

**Test:** `tests/memory/test_ask_memory.py` — stage order incl. `summarizing_memory` interleaved before
`token` (AC-29); concurrent same-session ask → 409 (AC-31); simulated mid-stream disconnect persists no
assistant message (AC-12); summariser exception → answer still produced, `summarize_failed` logged
(AC-27).

---

### T11 — End-to-end memory behavior (sliding window + batching + budget)
Wire the seams and seed the multi-turn tests. Covers the four headline acceptance scenarios against a
live DB + monkeypatched LLM.

**Test:** `tests/memory/test_ask_memory.py` (cont.) — **8-turn seed**: turn-9 prompt contains exactly
the last 5 pairs verbatim, none older, pairs 1–3 only in the summary, a turn-1-topic question still
resolves (AC-18/19/24). **Lazy-batch**: turns 1–8 → exactly one summariser call, `summarized_upto_message_id`
advances (AC-23). **Over-budget**: seeded 50k+ session → prompt = summary + exactly last 2 pairs,
`window_pairs==2`, `summarized True`, effective tokens < budget (AC-20/25). **Follow-up**: "BS admission
deadline?" then "aur MPhil ka?" → condensed query mentions MPhil, answer cites MPhil sources (AC-17).

---

### T12 — Toggle parity + async guard + CI job
Prove `ENABLE_MEMORY=false` (and missing `session_id`) is byte-for-byte `f9-cache-after` single-turn.
Add `tests/memory/test_no_sync_calls.py` (F9 style) and the `memory:` CI job (design §9.1).

**Test:** `tests/memory/test_toggle_parity.py` — SSE event sequence for a memory-off ask equals the
pre-F17 stateless sequence exactly (AC-33); `test_no_sync_calls.py` bans sync twins over `app/memory`
(AC-32).

---

### T13 — 10-dialogue follow-up quality set
Author `tests/fixtures/memory/followups.jsonl` — 10 two-turn dialogues where turn 2 is a bare follow-up
(code-switched included: "aur MPhil ka?", "iska deadline?"). `tests/memory/test_followups.py` runs each
through the memory pipeline and asserts turn-2 retrieval resolves the referent (condensed query contains
the turn-1 subject; answer cites a plausibly-correct doc). This is F17's quality signal, separate from
the F4 suites (AC-30).

**Test:** the follow-up set passes; committed under `tests/fixtures/memory/`.

---

### T14 — No-migration assertion
Confirm the whole feature added zero schema. `tests/memory/test_no_migration.py` (style of
`tests/cache/test_migration_0003.py` but inverted) asserts `alembic upgrade head` then
`alembic revision --autogenerate` yields an **empty** diff (AC-35). If it is non-empty, a column crept
in — resolve by derivation (design §2), do not commit a revision.

**Test:** autogenerate diff is empty.

---

### T15 — Eval gate (`f17-memory-after`) — THE DEFINITION OF DONE
Run the F4 latency + cost suites (memory-off harness, so the delta is ≈flat by construction — that is
the point: memory must not regress the shared path). `--compare f9-cache-after`. Then run the
10-dialogue follow-up set and record its resolution rate.

```bash
cd backend
# reseed corpus first — the pytest suite truncates documents/chunks (memory: test-suite-wipes-corpus)
python -m app.evals.run --suite latency --flags memory=off --label "f17-memory-after"
python -m app.evals.run --compare f9-cache-after
```

Commit `docs/eval_results/f17-memory-after.md` and
`docs/eval_results/f17-memory-after-vs-f9-cache-after.md` (p50/p95 + cost/query delta vs
`f9-cache-after`, expected ≈flat, plus the follow-up set result), each mapping the label → git SHA +
index manifest (AC-36).

**Definition of done:** all acceptance criteria in `requirements.md §4` pass AND both eval-result files
are committed. Until the delta report exists, F17 is not done (CLAUDE.md core philosophy #2).
</content>
