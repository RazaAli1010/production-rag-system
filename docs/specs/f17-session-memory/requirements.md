# F17 — Session Memory & Chat Experience · requirements.md

**Module:** `backend/app/memory/` · **Phase:** C (production layer) · **Depends on:** F12
(`sessions`, `messages`), F3 (pipeline + SSE + prompt seam), F7 (standalone-question condensation),
F10 (optional user binding) · **Flag:** `ENABLE_MEMORY` (default `false`) · **Model:** none new —
reuses `gpt-4o-mini` for summarization · **Eval gate:** `f17-memory-after` vs `f9-cache-after`
(latency/cost suites only) · **Blocks:** F11 final form, F14 chat UI

---

## 1. Overview

F17 turns single-shot Q&A into a **Claude-style chat**. Every conversation becomes a **session**
with **short-term memory**, so a follow-up like "aur MPhil ka?" resolves against turn 1's "BS
admission deadline?" instead of being answered blind. The user also **sees what the pipeline is
doing** through live streamed `stage` events (rewriting → searching → reranking → generating).

The memory model is the classic chatbot **sliding window + summarization hybrid**, additionally
**token-budgeted**:

1. **Sliding window (always on).** The prompt carries at most the **last `MEMORY_WINDOW_PAIRS=5`
   Q/A pairs verbatim** + the current question. As each turn completes the window slides and the
   oldest pair drops out of the prompt. The prompt NEVER contains the full transcript.
2. **Rolling summary of slid-out pairs (lazy, batched).** Pairs that fall out are not forgotten —
   they accumulate as "pending", and once **`MEMORY_SUMMARIZE_EVERY_PAIRS=3`** have accumulated the
   next turn folds them into a rolling `gpt-4o-mini` summary (one call per 3 pairs, not per turn).
3. **Token budget = hard cap.** When a session's cumulative tokens cross **`MEMORY_TOKEN_BUDGET=50_000`**,
   the verbatim window shrinks from 5 pairs to **`MEMORY_KEEP_LAST_PAIRS=2`**; prompt context from
   then on = summary + last 2 pairs + current question, and nothing older.

Everything is async; nothing in the memory path may block token streaming.

### 1.1 Design decisions resolved in the feature brief (do NOT re-derive)

- **The schema already exists.** F12's `0001_initial.py` created `sessions` and `messages` with every
  column F17 uses (`total_tokens`, `summary`, `summary_token_count`, `summarized_upto_message_id`,
  `last_active_at`, `title`, `is_archived`). **F17 adds NO Alembic migration** — the "over-budget
  marker" and "pending pair count" are *derived* from these columns, not stored in new ones (design §3).
- **The pipeline seam already exists.** `baseline._pipeline_events` / `astream` / `answer` already
  accept `memory: MemoryContext | None` and construct `AnswerResponse` with a `session_id` slot; F3's
  prompt already renders `MemoryContext` via `prompt.render_memory_block`, and F7's `rewrite` already
  condenses when memory is present. F17 **populates** these seams — it does not re-plumb them.
- **History is context, never a citation source.** Conversation history steers dialogue coherence
  only. The "answer ONLY from retrieved context, cite `[n]`" rule is unchanged; a `[n]` may only
  point at a retrieved chunk (US-6, AC-14).
- **The cache key is F7's standalone question, not the raw follow-up.** Memory and the F9 cache compose
  safely because F7 condenses the follow-up to a standalone question before both retrieval and the
  cache lookup — a follow-up can never poison the cache with conversation state (AC-17).
- **The F4 harness runs memory-off.** Every retrieval/RAGAS/refusal run passes `session_id=None`, so
  retrieval metrics stay comparable across every label. F17's own quality signal is a separate
  10-dialogue follow-up set (AC-30).
- **`/api/ask` does not exist yet.** Phase C build order is F9 → F10 → **F17** → F11, so F17 is the
  feature that first introduces the streaming `/api/ask` route (session-aware, write-behind,
  disconnect-safe). F11 later *hardens* it (validation, rate limit, request logging) — exactly as F9
  shipped the `flags.cache` bypass that F11 will map to an HTTP field. F17 does not implement rate
  limiting or request-log writing (design §1, out of scope).

## 2. User stories

**US-1 (Student):** As a student, I want my follow-up "aur MPhil ka?" to be understood as being about
admission deadlines, so I do not have to repeat the whole question every turn.

**US-2 (Student):** As a student on mobile, I want to see "searching… reranking… generating…" while I
wait, so a 4-second answer feels alive instead of frozen.

**US-3 (Student):** As a student in a very long chat, I want the assistant to still remember what we
established at the start, so the conversation stays coherent even after 30 turns.

**US-4 (Anonymous visitor):** As someone not logged in, I want chat to just work, so I can try the
assistant with zero friction — my session lives in a signed cookie, capped and expiring, not lost on
every message.

**US-5 (Logged-in student):** As a returning user, I want my past sessions listed newest-first and
re-openable, so I can pick up where I left off.

**US-6 (Compliance / product owner):** As the owner, I want conversation history to be strictly
non-citable, so the assistant can never "cite" something a user said instead of an official document.

**US-7 (Ops / cost owner):** As the person paying the OpenAI bill, I want the summarizer to fire at
most once per 3 slid-out pairs, so memory adds bounded, amortized cost rather than an LLM call per
turn.

**US-8 (Ops):** As an operator, I want memory switchable off in prod without a deploy, so a misbehaving
memory path is one flag away from stateless v2 behavior.

**US-9 (Ops):** As an operator, I want a client that disconnects mid-answer to leave no half-written
assistant message, so a dropped mobile connection never corrupts a transcript.

**US-10 (Eval owner):** As the eval owner, I want the retrieval/RAGAS/refusal suites to be structurally
memory-off, so hit@k and faithfulness stay comparable across every label, while a small follow-up set
proves memory resolves references.

## 3. EARS acceptance criteria

### 3.1 Sessions & anonymous access

- **AC-1 (Event-driven — create):** When `POST /api/sessions` is called, the system shall create a
  `sessions` row and return its id; for an authenticated caller the row's `user_id` shall be the
  caller, and for an anonymous caller `user_id` shall be `NULL` and the id shall be returned in a
  signed, `httpOnly` cookie.
- **AC-2 (Event-driven — auto title):** When the first user question is persisted to a session with a
  `NULL` title, the system shall set `sessions.title` to that question truncated to
  `MEMORY_SESSION_TITLE_MAX_CHARS=60` characters.
- **AC-3 (State-driven — list):** While the caller is authenticated, `GET /api/sessions` shall return
  that user's non-archived sessions ordered by `last_active_at` descending.
- **AC-4 (Event-driven — transcript):** When `GET /api/sessions/{id}/messages` is called by the
  session's owner, the system shall return the **full** message list in `created_at` order — the
  sliding window limits only the LLM prompt, never what the UI renders.
- **AC-5 (Event-driven — archive):** When `DELETE /api/sessions/{id}` is called by the owner, the
  system shall set `is_archived=true` (soft delete) and the session shall no longer appear in AC-3.
- **AC-6 (Unwanted — ownership):** If a caller requests a session they do not own, the system shall
  respond `404` (not `403`) so session existence is not an enumeration oracle.
- **AC-7 (State-driven — anonymous caps):** While a session is anonymous, it shall be capped at
  `MEMORY_ANON_MAX_MESSAGES=30` messages and `MEMORY_ANON_TTL_DAYS=7` of inactivity; the cap is
  enforced at ask-time and the TTL is pruned by the F12 cleanup job (this spec only sets the values).
- **AC-8 (Unwanted — cookie forgery):** If an anonymous session cookie fails signature verification,
  the system shall ignore it and treat the request as having no session.

### 3.2 Ask integration & message persistence

- **AC-9 (Event-driven — optional session):** When `/api/ask` is called with a `session_id`, memory on,
  and the session exists and is owned by the caller, the pipeline shall load memory for that session;
  when `session_id` is absent it shall behave as a stateless single turn.
- **AC-10 (Ubiquitous — user write first):** The system shall persist the user question to `messages`
  (with its `tiktoken cl100k_base` `token_count`) BEFORE running the pipeline, so its `created_at`
  sorts strictly before the assistant reply.
- **AC-11 (Event-driven — assistant write-behind):** When the SSE stream terminates cleanly with a
  `done` event, the system shall persist the assistant message via `asyncio.create_task` (write-behind)
  with `refused` reflecting the answer and its `citations` serialized, holding a strong reference to
  the task.
- **AC-12 (Unwanted — mid-stream disconnect):** If the client disconnects before `done`, the system
  shall NOT persist a partial assistant message.
- **AC-13 (Ubiquitous — token accounting):** On every message write the system shall increment
  `sessions.total_tokens` atomically by that message's `token_count`; `total_tokens` shall be the
  authoritative running sum of the whole conversation. Counting is pure CPU and runs inline.

### 3.3 Prompt integration

- **AC-14 (Ubiquitous — non-citable history):** The assembled prompt shall present conversation history
  as dialogue context explicitly marked non-citable; the "answer only from retrieved context, cite
  `[n]`" rule shall be unchanged, and a `[n]` shall only ever resolve to a retrieved chunk.
- **AC-15 (Ubiquitous — MemoryContext seam):** Memory assembly shall return the canonical
  `MemoryContext`, which the F3 chain consumes through its existing `prompt.render_memory_block` seam
  with no change to any retrieval contract.
- **AC-16 (Ubiquitous — session id surfaced):** `AnswerResponse.session_id` shall carry the session id
  for a memory-on turn (currently hardcoded `None`), and `AnswerResponse.memory_summarized` shall equal
  `MemoryContext.summarized`.

### 3.4 Memory window assembly — the core rule

- **AC-17 (Event-driven — follow-up condensation):** When memory is populated, F7's `rewrite` shall
  condense the follow-up into a standalone question, and BOTH retrieval and the F9 cache key shall use
  that standalone question — memory never changes the cache key contract.
- **AC-18 (State-driven — verbatim window ≤5 pairs):** While a session has `≤ MEMORY_WINDOW_PAIRS=5`
  pairs, the prompt context shall be all pairs verbatim + the current question, with no summary.
- **AC-19 (State-driven — windowed + summary):** While a session has `> 5` pairs and
  `total_tokens < MEMORY_TOKEN_BUDGET`, the prompt context shall be the rolling summary + the last 5
  pairs verbatim + the current question; `MemoryContext.window_pairs` shall be `5`.
- **AC-20 (State-driven — over-budget window):** While `total_tokens ≥ MEMORY_TOKEN_BUDGET=50_000`, the
  prompt context shall be the rolling summary + the last `MEMORY_KEEP_LAST_PAIRS=2` pairs + the current
  question and nothing older; `MemoryContext.window_pairs` shall be `2` and `MemoryContext.summarized`
  shall be `true`.
- **AC-21 (Ubiquitous — whole pairs):** The system shall keep Q/A pairs whole — a user message and its
  assistant reply enter and leave the window together — even when one message is very large.
- **AC-22 (Ubiquitous — O(window) reads):** Memory load shall be a single indexed Postgres round trip
  reading the session row plus only the last-`window` messages (`ORDER BY created_at DESC LIMIT`), not
  the whole transcript.

### 3.5 Rolling summarization

- **AC-23 (Event-driven — lazy batch trigger):** When `MEMORY_SUMMARIZE_EVERY_PAIRS=3` pairs have slid
  out of the window and remain unsummarized, the NEXT turn — BEFORE retrieval — shall run exactly one
  `gpt-4o-mini` summarization call (temp `0`, `MEMORY_SUMMARY_MAX_TOKENS=600`) that EXTENDS the rolling
  summary (old summary + pending pairs → new summary), and shall advance
  `sessions.summarized_upto_message_id`, `sessions.summary`, and `sessions.summary_token_count`.
- **AC-24 (Ubiquitous — never re-summarize whole transcript):** The summarizer shall only consume the
  old summary plus the not-yet-summarized pending pairs — never the full transcript.
- **AC-25 (Event-driven — budget-crossing immediate fold):** When `total_tokens` crosses
  `MEMORY_TOKEN_BUDGET`, the next turn shall fold any not-yet-covered older pairs into the summary
  immediately (no batching delay) before assembling the shrunken 2-pair window.
- **AC-26 (Ubiquitous — summarize stage):** A `stage summarizing_memory started/done` event (with `ms`
  on `done`) shall stream when a summarization call runs, so the user sees why the first token is
  slightly delayed; when no summarization runs this turn the stage shall report `skipped`.
- **AC-27 (Unwanted — summarizer failure):** If the summarization call fails or times out
  (`MEMORY_SUMMARY_TIMEOUT_S`), the system shall log `memory.summarize_failed`, leave the pending pairs
  pending (retried next trigger), assemble the turn with the verbatim window only, and answer normally
  — a summary failure shall never block answering.
- **AC-28 (Unwanted — refusals in history):** Refused turns shall be kept in the window and marked, and
  shall be excluded from the summary's "facts asked/answered" content.

### 3.6 Live pipeline stage events

- **AC-29 (Ubiquitous — ordered stages):** Every pipeline seam shall emit paired `stage` events in
  order `summarizing_memory → rewriting → cache_lookup → searching → reranking → compressing →
  generating → citing`, each `done` carrying elapsed `ms`, each flag-off seam reporting `skipped`;
  stage events shall interleave before `token` events on the same SSE stream and their emission shall
  be fire-and-forget so a slow client never stalls generation.

### 3.7 Eval isolation, concurrency, async mandate, toggling & gate

- **AC-30 (State-driven — eval isolation):** While the F4 harness runs any suite, it shall pass
  `session_id=None` (memory off) so retrieval/RAGAS/refusal metrics stay comparable across every label;
  F17's own reference-resolution quality shall be measured by a separate committed 10-dialogue
  two-turn follow-up set, not by the standard suites.
- **AC-31 (Unwanted — concurrent asks):** If a second `/api/ask` arrives for a session whose per-session
  `asyncio.Lock` is held, the system shall respond `409 session_busy` rather than interleave two turns.
- **AC-32 (Ubiquitous — async surface):** All memory I/O shall be async: async SQLAlchemy for
  `sessions`/`messages`, `ainvoke` for the summarizer; message writes are write-behind
  (`asyncio.create_task`); `tiktoken` counting is cheap pure-CPU and runs inline.
- **AC-33 (State-driven — toggle parity):** While `ENABLE_MEMORY` is `false` OR `session_id` is absent,
  `/api/ask` behavior shall be byte-for-byte identical to `f9-cache-after` single-turn — no memory load,
  no persistence, no `summarizing_memory` stage — proved by a regression test.
- **AC-34 (Ubiquitous — Settings centralisation):** Every new config value (`ENABLE_MEMORY`, `MEMORY_*`)
  shall live in the single `app.core.settings.Settings` class with no module reading `os.environ`.
- **AC-35 (Ubiquitous — no migration):** F17 shall add NO Alembic revision; it shall use only the
  `sessions`/`messages` columns F12 already migrated, and `alembic revision --autogenerate` shall stay
  empty. Any need for a new column is a spec bug to resolve by derivation, not a migration.
- **AC-36 (Ubiquitous — eval gate):** F17 shall not be done until
  `docs/eval_results/f17-memory-after.md` and
  `docs/eval_results/f17-memory-after-vs-f9-cache-after.md` are committed (latency/cost suites only),
  mapping the label to a git SHA + index manifest, plus the 10-dialogue follow-up set results.

## 4. Acceptance criteria (feature-level definition of done)

1. **Follow-up test:** turn 1 "BS admission deadline?", turn 2 "aur MPhil ka?" → the condensed
   standalone query mentions MPhil and the answer cites MPhil sources (AC-17).
2. **Sliding-window test:** a seeded 8-turn session → the LLM prompt for turn 9 contains exactly the
   last 5 Q/A pairs verbatim and none older; pairs 1–3 appear only inside the rolling summary; a
   question referencing turn 1's topic still resolves via the summary (AC-18/19/24).
3. **Lazy-batch test:** turns 1–8 produce exactly one summarization call (not three), and
   `summarized_upto_message_id` advances correctly (AC-23).
4. **Over-budget test:** a seeded 50k+-token session → the next `/api/ask` prompt contains summary +
   exactly the last 2 Q/A pairs and nothing older, `MemoryContext.window_pairs == 2`,
   `summarized == true`, and effective prompt tokens drop below the budget (AC-20/25).
5. **Stage-order test:** `stage` events arrive in pipeline order, interleaved before `token` events,
   each `done` carrying `ms`, flags-off stages reporting `skipped` (AC-29).
6. **Toggle-parity test:** `ENABLE_MEMORY=false` or missing `session_id` is byte-identical to
   `f9-cache-after` single-turn (AC-33).
7. **Disconnect test:** a mid-stream disconnect persists no partial assistant message (AC-12).
8. **Concurrency test:** a second concurrent ask on the same session returns `409 session_busy` (AC-31).
9. **No-migration check:** `alembic revision --autogenerate` yields an empty diff (AC-35).
10. **Eval gate:** `f17-memory-after` vs `f9-cache-after` delta report committed, plus the 10-dialogue
    follow-up set results (AC-36).

## 5. Out of scope (do not implement here)

- **Long-term / cross-session memory** ("remembers you between chats"), user-editable memory, retrieval
  over past conversations, vector search over message history, multi-device sync beyond login — all v3
  talking points.
- **`/api/ask` hardening** — rate limiting, request validation middleware, and `request_logs` writing
  are F11/F13. F17 ships the session-aware streaming route those features wrap.
- **The anonymous-session TTL pruning job** — F12 owns the cleanup job; F17 only sets the cap/TTL values.
- **Frontend chat rendering** — F14 consumes the SSE `stage`/`token` stream and the sessions endpoints;
  F17 only produces them.
- **New DB columns / any schema change** — F12 already migrated `sessions`/`messages` (AC-35).
</content>
