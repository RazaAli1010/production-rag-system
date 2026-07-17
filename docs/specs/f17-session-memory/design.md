# F17 — Session Memory & Chat Experience · design.md

**Module:** `backend/app/memory/` · **Phase:** C · **Depends on:** F12, F3, F7, F10 · **Flag:**
`ENABLE_MEMORY` · **Model:** none new (`gpt-4o-mini` summarizer) · **Eval gate:** `f17-memory-after`
vs `f9-cache-after`

---

## 1. Module layout

```
backend/app/memory/                    NEW package
├── __init__.py                        NEW  (empty)
├── tokens.py                          NEW  count(text) via tiktoken cl100k_base  (pure CPU)
├── window.py                          NEW  assemble(session, messages, settings) -> MemoryContext
│                                           the sliding-window + budget rule, pure function
├── summarizer.py                      NEW  extend_summary(old, pending, settings)  (1 gpt-4o-mini call)
├── service.py                         NEW  async DB ops: create/list/get/archive session,
│                                           persist_user / persist_assistant (write-behind),
│                                           load_memory (1 round trip), per-session Lock registry
├── stages.py                          NEW  the single stage-event emitter wrapping astream_events
└── cookies.py                         NEW  sign/verify anon session id (reuses core.security pyjwt)

backend/app/api/
├── ask.py                             NEW  POST /api/ask — session-aware SSE StreamingResponse
└── sessions.py                        NEW  POST/GET/DELETE sessions REST router

backend/app/rag/
├── baseline.py                        CHANGED  session_id + memory_summarized surfaced onto
│                                               AnswerResponse; langfuse_handler(session_id=...);
│                                               summarizing_memory stage emitted before rewrite
└── (prompt/rewrite/events/...)        UNCHANGED  seams already present (render_memory_block,
                                                  rewrite(memory=...), StageEvent shape)

backend/app/core/settings.py           CHANGED  + "Session memory (F17)" block
backend/app/core/contracts.py          CHANGED  + MemoryContext.window_pairs / .effective_tokens
backend/app/main.py                    CHANGED  include ask + sessions routers
backend/app/db/models/chat.py          UNCHANGED  Session/Message already model everything (F12)
backend/app/db/migrations/             UNCHANGED  NO new revision (design §3)

backend/tests/memory/                  NEW  conftest, test_tokens, test_window, test_summarizer,
                                            test_service, test_sessions_api, test_ask_memory,
                                            test_stages, test_toggle_parity, test_no_sync_calls,
                                            test_settings_schemas, test_followups (10-dialogue set)
backend/tests/fixtures/memory/
└── followups.jsonl                    NEW  10 two-turn dialogues (AC-30 quality signal)
.github/workflows/ci.yml               CHANGED  NEW `memory:` job (mirrors `caching:`) — §9.1
```

## 2. Key design decision: nothing new is stored, everything is derived

F12's `0001_initial.py` already created `sessions` and `messages` with the exact columns F17 needs.
The temptation is to add bookkeeping columns — a `needs_summarize` flag, a `pending_pairs` counter, a
message `seq`. **None are needed**, and each would be an Alembic migration and a source of drift:

| Wanted state | Naive column | Derived instead (chosen) |
|---|---|---|
| "am I over budget?" | `sessions.needs_summarize bool` | `total_tokens >= MEMORY_TOKEN_BUDGET` — already the authoritative sum (AC-13) |
| "how many pairs are pending summary?" | `sessions.pending_pairs int` | count of messages with `created_at >` the row pointed at by `summarized_upto_message_id`, minus the window — one indexed query |
| "which pairs are inside the summary?" | new column | `summarized_upto_message_id` (F12) is exactly this pointer |
| "user msg sorts before assistant msg" | `messages.seq int` | user message is written BEFORE the pipeline runs (AC-10); its `created_at` is strictly earlier than the write-behind assistant message |

So **F17 adds no migration** (AC-35). This is the single most important design property of the feature:
the state machine lives in code (`window.py`), reading columns F12 owns.

`ponytail:` `created_at` ordering assumes the user write commits before the assistant write, which the
pipeline guarantees (user-first, assistant write-behind after `done`). If two writes ever share a
timestamp under coarse clock resolution, add a monotonic `seq` — but not before it is observed.

## 3. Data flow

```
POST /api/ask {question, session_id?}
   │
   ├─ resolve principal (F10 optional) + anon cookie (cookies.verify)
   ├─ ENABLE_MEMORY off OR no session_id ─────────────► stateless astream()  [== f9-cache-after]
   │
   ├─ acquire per-session asyncio.Lock ── held? ──► 409 session_busy            (AC-31)
   │
   ├─ tokens.count(question); service.persist_user(...)  (user msg FIRST, total_tokens +=)  (AC-10/13)
   │
   ├─ mem = service.load_memory(session)   ── 1 indexed round trip: session row
   │        + last-`window` messages (ORDER BY created_at DESC LIMIT)          (AC-22)
   │   └─ window.assemble(...) decides:
   │        pending_pairs >= 3  OR  crossed 50k  ──► summarizer.extend_summary()  (AC-23/25)
   │                                                 emit stage summarizing_memory
   │        build MemoryContext(summary, pairs, window_pairs, effective_tokens, summarized) (AC-18/19/20)
   │
   ├─ astream(question, memory=mem, session_id=session.id, ...)   ← F3 pipeline, unchanged seams
   │        emits: stage* (summarizing_memory..citing) → token* → citations → meta → done|error
   │
   └─ on clean `done`: service.persist_assistant(...) via create_task (write-behind)  (AC-11)
      on disconnect before `done`: no assistant write                                 (AC-12)
```

### 3.1 `window.assemble` — the core rule (pure function, no I/O)

```python
def assemble(session: Session, recent: list[Message], pending: list[Message],
             settings) -> MemoryContext:
    """recent = last-`window` messages already ordered oldest→newest (service loads them).
    pending = slid-out messages not yet in the summary (service supplies the slice).
    Pure CPU: no DB, no LLM. The summariser is called by the caller, not here — this function
    only decides the SHAPE of the turn's context.
    """
    over_budget = session.total_tokens >= settings.MEMORY_TOKEN_BUDGET
    window_pairs = settings.MEMORY_KEEP_LAST_PAIRS if over_budget else settings.MEMORY_WINDOW_PAIRS
    pairs = _last_whole_pairs(recent, window_pairs)          # keeps user+assistant together (AC-21)
    summary = session.summary                                # None when ≤5 pairs & never summarised
    return MemoryContext(
        summary=summary,
        pairs=[ChatMessage(role=m.role, content=m.content) for m in pairs],
        window_pairs=window_pairs,
        effective_tokens=(session.summary_token_count or 0) + sum(m.token_count for m in pairs),
        summarized=over_budget,
    )
```

The summarization *decision* (does this turn need a summariser call, and over which pending pairs) is
computed in `service.load_memory` from `summarized_upto_message_id`; `assemble` only shapes context.

### 3.2 `summarizer.extend_summary` — one LLM call, amortized

```python
async def extend_summary(old_summary: str | None, pending: list[Message], settings) -> str:
    """old + pending → new rolling summary. temp 0, max_tokens=MEMORY_SUMMARY_MAX_TOKENS=600.
    Consumes ONLY old + pending (never the whole transcript, AC-24). Refused turns contribute
    their questions but are excluded from the 'facts answered' section (AC-28).
    Raises on failure — caller catches, logs memory.summarize_failed, proceeds window-only (AC-27).
    """
```

Prompt: "Extend this running summary of a student–assistant chat. Record facts asked, answers given,
documents cited, and unresolved threads. Keep under N tokens. Do not invent." Reuses `ChatOpenAI`
(async `ainvoke`) exactly like `rewrite.py`.

### 3.3 `service` — async DB surface

```python
async def create_session(db, *, user_id: uuid.UUID | None) -> Session
async def list_sessions(db, *, user_id: uuid.UUID) -> list[Session]      # last_active_at DESC, not archived
async def get_owned(db, session_id, *, principal) -> Session             # 404 on miss/foreign (AC-6)
async def get_messages(db, session_id) -> list[Message]                  # FULL transcript (AC-4)
async def archive(db, session_id) -> None                                # is_archived=true (AC-5)

async def persist_user(db, session, question: str) -> Message            # BEFORE pipeline; sets title
                                                                         # if NULL; total_tokens += (AC-2/10/13)
def schedule_persist_assistant(session_id, response: AnswerResponse, *, sessionmaker) -> None
                                                                         # create_task write-behind (AC-11)
async def load_memory(db, session, settings) -> tuple[MemoryContext, list[Message] | None]
                                                                         # returns ctx + pending-to-summarise
                                                                         # (None when no summariser call due)
```

Per-session lock: a process-local `dict[uuid, asyncio.Lock]` (`ponytail:` in-process registry, one
API replica; a second replica would let two nodes run the same session concurrently — revisit with the
F9 multi-replica note). Acquired non-blocking (`lock.locked()` → `409`), released in `finally`.

Write-behind mirrors F9's `store.schedule_write`: `create_task` on the app-wide `sessionmaker` (not the
request session, which closes when the stream ends), strong ref held in a module set with a
done-callback that discards it and logs on exception (AC-11, the canonical create_task footgun).

### 3.4 `stages.py` — one emitter, not eight hand-rolled

The stage vocabulary and `StageEvent` shape already exist (`core.contracts.StageEvent`,
`rag.events.stage_event`). F3 already emits `searching/generating/citing`. F17 adds the
`summarizing_memory` stage at the pre-retrieval seam and provides `stages.emit(...)` as the single
helper the ask route uses, so F14/Langfuse (F13) derive from one instrumentation point rather than each
feature hand-rolling events (per CLAUDE.md "single F17 emitter `app/memory/stages.py`"). Emission is
fire-and-forget over a bounded `asyncio.Queue`; a full queue drops the stage event, never blocks a
`token` (AC-29).

## 4. `/api/ask` and sessions routers

`api/ask.py` — `POST /api/ask` returns a `StreamingResponse` (media type `text/event-stream`) whose
generator is the memory wrapper above. Session binding, the per-session lock, write-behind, and
disconnect detection (`await request.is_disconnected()` gating the assistant write) live here. F11 later
adds validation/rate-limit/request-log middleware around it — F17 does not (§ out of scope).

`api/sessions.py` — `POST /` (AC-1), `GET /` (AC-3, authed), `GET /{id}/messages` (AC-4),
`DELETE /{id}` (AC-5). Ownership via `service.get_owned` → `404` on foreign (AC-6). Anonymous create
issues the signed cookie via `cookies.sign`.

`cookies.py` — `sign(session_id)` / `verify(cookie) -> uuid | None` using `pyjwt` with the existing
`JWT_SECRET` and a dedicated `typ="anon_session"` claim (no new dependency — reuses `core.security`'s
encode/decode surface). Bad signature → `None` → treated as no session (AC-8).

## 5. Contract changes (additive, no migration)

`core.contracts.MemoryContext` gains two fields, additive with defaults exactly like F9's
`AnswerResponse.tokens_in/out` — `prompt.render_memory_block` and `rewrite._build_messages` already
consume `summary` + `pairs` and are unaffected:

```python
class MemoryContext(BaseModel):
    summary: str | None = None
    pairs: list[ChatMessage] = []
    summarized: bool = False
    window_pairs: int = 0        # NEW — window size actually used this turn (AC-19/20 assert on it)
    effective_tokens: int = 0    # NEW — tokens of summary + pairs (over-budget test asserts < budget)
```

`baseline._pipeline_events` stops hardcoding `session_id=None` / `memory_summarized=False` at the two
`AnswerResponse` construction sites, taking them from the incoming `session_id` arg and
`memory.summarized`; `langfuse_handler(session_id=None)` becomes `session_id=session_id` (F13 spans then
group by session for free). `astream`/`answer` gain a `session_id: str | None = None` keyword. No other
pipeline change — retrieval/rerank/compress/cache seams are untouched (AC-15).

## 6. Error handling

| Failure | Handling | AC |
|---|---|---|
| Summariser call raises/timeout | catch, log `memory.summarize_failed`, keep pending pending, assemble window-only, answer | AC-27 |
| Client disconnects before `done` | `request.is_disconnected()` gate → skip assistant write; user message already persisted stands | AC-12 |
| Concurrent ask, lock held | `409 session_busy` (JSON) before any write | AC-31 |
| Foreign / missing session | `404` from `service.get_owned` | AC-6 |
| Forged anon cookie | `cookies.verify` returns `None` → no session | AC-8 |
| Anon session over 30 msgs | ask-time cap check → `409`/refuse per AC-7 | AC-7 |
| Memory off / no session_id | early return to stateless `astream` — no memory code runs | AC-33 |

Memory is an enhancement, never a failure source: any memory-load DB error logs `memory.load_failed`
and falls back to a stateless turn (the answer is still produced), same posture as F9's cache-degraded.

## 7. New Settings keys (`# --- Session memory (F17) ---`)

```python
ENABLE_MEMORY: bool = False              # prod/request toggle; False ≡ f9-cache-after single-turn (AC-33)
MEMORY_TOKEN_BUDGET: int = 50_000        # hard cap; crossing it shrinks the window (AC-20)
MEMORY_WINDOW_PAIRS: int = 5             # verbatim window under budget (AC-18/19)
MEMORY_KEEP_LAST_PAIRS: int = 2          # shrunken window once over budget (AC-20)
MEMORY_SUMMARIZE_EVERY_PAIRS: int = 3    # lazy-batch trigger for the summariser (AC-23)
MEMORY_SUMMARY_MAX_TOKENS: int = 600     # summary output cap (AC-23)
MEMORY_SUMMARY_MODEL: str = "gpt-4o-mini"   # summariser LLM (project primary; NOT gpt-4o deep mode)
MEMORY_SUMMARY_TEMPERATURE: float = 0.0  # deterministic summary
MEMORY_SUMMARY_TIMEOUT_S: float = 8.0    # summariser timeout → window-only fallback (AC-27)
MEMORY_SESSION_TITLE_MAX_CHARS: int = 60 # auto-title cap (AC-2)
MEMORY_ANON_MAX_MESSAGES: int = 30       # anonymous session message cap (AC-7)
MEMORY_ANON_TTL_DAYS: int = 7            # anonymous inactivity TTL, pruned by F12 job (AC-7)
```

All in the single `Settings` class; nothing reads `os.environ` (AC-34). `MEMORY_TOKEN_BUDGET`,
`MEMORY_WINDOW_PAIRS`, `MEMORY_KEEP_LAST_PAIRS`, `MEMORY_SUMMARIZE_EVERY_PAIRS`,
`MEMORY_SUMMARY_MAX_TOKENS` match the CLAUDE.md canonical defaults verbatim.

## 8. Alembic migration

**None.** F12's `0001_initial.py` created `sessions` and `messages` with every column used here
(design §2). AC-35 asserts `alembic revision --autogenerate` produces an empty diff after F17 lands. If
review finds a genuinely un-derivable state, resolve it by derivation first; a migration is a last
resort and a spec amendment, not a silent addition.

## 9. Honoring Shared Context contracts & the F3 retriever seam

- **`MemoryContext`** is the canonical cross-feature model; F17 assembles it, F3 renders it via the
  existing `prompt.render_memory_block` seam, F7 condenses via the existing `rewrite(memory=...)`
  argument — **no retrieval contract changes** (AC-15). The retriever seam from F3 is untouched: memory
  reaches retrieval only as the condensed standalone query F7 already produces.
- **SSE contract** is preserved: F17 only ADDS the `summarizing_memory` stage at the front of the
  existing ordered `stage* → token* → citations → meta → done|error` sequence; no new event type
  (AC-29). F14 needs no contract change.
- **F9 cache** composes because both retrieval and the cache key use F7's standalone question (AC-17);
  the F4 harness runs `session_id=None` so cache/retrieval metrics stay label-comparable (AC-30).
- **`AnswerResponse`** gains no field — `session_id` and `memory_summarized` already exist and were
  hardcoded; F17 populates them (AC-16).

### 9.1 CI

A `memory:` job mirroring the `caching:` job: an async-guard block over `app/memory` (ban `.invoke(`,
`embed_query(`/`embed_documents(`, `import requests`, bare `import redis`, `create_engine(`) plus
`ruff check app/memory`, and `backend/tests/memory/test_no_sync_calls.py` enforcing the same in-suite —
CI gives each package its own guard block, so a new package needs a new job (F9 precedent).

## 10. Metrics logged (every metric named is logged)

`memory.summarize` (ms, pending_pairs_folded, summary_tokens), `memory.summarize_failed`,
`memory.load_failed`, `memory.session_busy`, plus the `stage summarizing_memory` timing on the SSE
stream. `sessions.total_tokens` is the running token metric. The eval gate reads latency/cost off the
existing SSE `meta` event (F9 plumbing) — memory adds no new eval metric, only the 10-dialogue
follow-up resolution result (AC-30/36).
</content>
