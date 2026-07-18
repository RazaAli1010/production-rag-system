# F14 — React Frontend (chat UI) · requirements.md

**Module:** `frontend/` · **Phase:** D (ship) · **Depends on:** F11 (OpenAPI + error envelope +
rate limiting), F10 (auth), F17 (sessions & memory), F13 (`/internal/stats`).

F14 adds **no backend code, no Settings key, and no Alembic migration**. It is a pure consumer of
the wire contract that F11 froze. Anywhere this document seems to ask for a backend behaviour that
does not exist today, it is listed in §9 (Out of scope / backend gaps) instead of being assumed.

---

## 1. The contract as it actually exists

These are read from the shipped backend, not from the feature brief. Where the two disagree, the
code wins and the disagreement is called out.

### 1.1 Endpoints consumed

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/api/ask` | optional | SSE by default; one `AnswerResponse` on `Accept: application/json` |
| `POST` | `/api/sessions` | optional | anon → sets signed httpOnly cookie |
| `GET` | `/api/sessions` | **required** | authed users only |
| `GET` | `/api/sessions/{id}/messages` | optional | full transcript; anon needs the cookie |
| `DELETE` | `/api/sessions/{id}` | optional | soft delete (archive) |
| `POST` | `/api/auth/register` | — | 201 → `UserOut` |
| `POST` | `/api/auth/token` | — | OAuth2 password form → `TokenResponse` |
| `POST` | `/api/auth/refresh` | — | `{refresh_token}` → `TokenResponse` |
| `POST` | `/api/auth/logout` | bearer | 204 |
| `GET` | `/api/auth/me` | bearer | `UserOut` (carries `role`) |
| `GET` | `/api/documents` | public | Sources page |
| `GET` | `/api/health` | public | boot-time degradation banner |
| `GET` | `/internal/stats?window=24h` | **admin** | stats cards |

### 1.2 `POST /api/ask` request body

```ts
{ question: string,          // 3..500 chars — BOTH bounds are server-enforced
  session_id?: string,       // uuid
  namespace?: "pu" | "hec",  // absent = all
  deep?: boolean,
  skip_cache?: boolean }     // flags_override is admin-only → the UI never sends it
```

### 1.3 SSE event order

`stage`\* → `token`\* → `citations` → `meta` → `done` | `error`

- `stage.data` = `{stage: string, status: "started"|"done"|"skipped", ms: int|null}`.
  **`stage` is a bare `str` in `core/contracts.py`, not a Literal** — the UI must render an
  unknown stage id rather than crash or drop it.
- `meta.data` = `AnswerResponse` **without `answer`**, including `request_id`, `latency_ms`,
  `refused`, `refusal_reason`, `degraded`, `cache_hit`, `pipeline_flags`, `session_id`,
  `memory_summarized`, `tokens_in`, `tokens_out`.
- `error` arrives **on a 200 stream** (the response has already committed) — including the
  server-side `REQUEST_TIMEOUT_S` timeout. There is no HTTP status to react to.

### 1.4 `Citation` shape

```ts
{ chunk_id, doc_id, title, section_heading: string|null,
  page_start: int|null, page_end: int|null,
  url: string|null,   // null for pre-LLM refusal suggestions
  quote: string }     // ≤ 25 words, always extracted, never LLM-authored
```
There is **no `anchor` field** on the shipped model (the Shared Context draft has one; the code does
not). The UI must not reference it.

### 1.5 Error shapes — there are two

| Source | Body |
|---|---|
| Registered handlers (422 / 429 / 503 / 504 / 500) | `{"error": {"type", "message", "request_id", "detail"?}}` |
| Raw `HTTPException` (403 `flags_override`, 404 session, **409 `session_busy`**) | `{"detail": "..."}` |

429 additionally carries a `Retry-After` header (seconds).

---

## 2. User stories

**US-1 — Ask without an account.** As a student who found the link in a WhatsApp group, I want to
ask a question immediately, so that I do not abandon at a signup wall.

**US-2 — Watch it work.** As a student who does not trust chatbots, I want to see what the system is
doing while it thinks, so that I can tell it is reading documents rather than inventing text.

**US-3 — Check the source.** As a student about to act on an answer, I want to tap a `[n]` marker
and see the document, section, page and the exact sentence, so that I can verify before I rely on it.

**US-4 — Ask a follow-up.** As a student mid-conversation, I want "aur agar CGPA 1.8 ho to?" to be
understood in context, so that I do not restate my whole situation.

**US-5 — Be refused honestly.** As a student asking about something outside the corpus, I want a
clear "not in these documents" with pointers, so that I do not mistake a guess for a rule.

**US-6 — Keep my chats.** As a returning student, I want to log in and reopen an old conversation,
so that I can find the fee-refund answer I got last week.

**US-7 — Ask in my own register.** As a student who types Urdu, Roman Urdu and English in one
sentence, I want the input and the answer to render correctly in both directions.

**US-8 — See coverage.** As a sceptical student, I want a list of the documents the assistant can
actually read, so that I know its limits before asking.

**US-9 — Operate it.** As an admin, I want latency, cost, cache-hit and refusal figures in one
screen, so that I can spot a regression without opening a SQL client.

**US-10 — Use it on a bad connection.** As a student on 3G with a 360px phone, I want the answer to
start appearing quickly and the page to stay usable.

---

## 3. EARS acceptance criteria

### 3.1 Chat & streaming

- **AC-1** WHEN the user submits a question of 3–500 characters, THE SYSTEM SHALL `POST /api/ask`
  with `Accept: text/event-stream` and render the response as an assistant turn in the thread.
- **AC-2** WHILE the question is shorter than 3 characters or longer than 500, THE SYSTEM SHALL keep
  the send control disabled and show the counter in its over/under state — the request is never sent.
- **AC-3** WHEN a `stage` event arrives, THE SYSTEM SHALL append or update that stage in the live
  status trail in arrival order, showing `ms` on `status:"done"` and a struck-through label on
  `status:"skipped"`.
- **AC-4** IF a `stage` event carries a `stage` id the UI has no label for, THEN THE SYSTEM SHALL
  render the raw id verbatim and continue streaming.
- **AC-5** WHEN the first `token` event arrives, THE SYSTEM SHALL collapse the status trail into a
  single summary chip reading `worked for <N.N>s`, expandable to the per-stage timings.
- **AC-6** WHILE `token` events arrive, THE SYSTEM SHALL append them to the assistant bubble without
  re-rendering prior text, and SHALL keep the view pinned to the bottom **only if** the user has not
  scrolled up; otherwise it SHALL show a "Jump to latest" control.
- **AC-7** WHEN the `citations` event arrives, THE SYSTEM SHALL resolve every inline `[n]` marker
  already streamed into a tappable citation chip, and SHALL resolve later markers as they stream.
- **AC-8** IF a `[n]` marker has no matching citation at index `n`, THEN THE SYSTEM SHALL render it
  as plain text, never as a dead control.
- **AC-9** WHEN the `meta` event arrives with `degraded: true`, THE SYSTEM SHALL show an inline
  "Searched keyword index only" note on that turn.
- **AC-10** WHEN `done` arrives, THE SYSTEM SHALL re-enable the composer and stop the aria-live
  region from announcing.
- **AC-11** THE SYSTEM SHALL send `namespace` only when a chip other than "All" is selected.

### 3.2 Citations

- **AC-12** WHEN a citation chip is activated, THE SYSTEM SHALL open the citation panel — a
  bottom sheet below 768px, a side panel at or above — showing title, section heading, page range,
  and the quote.
- **AC-13** IF `citation.url` is non-null AND `page_start` is non-null AND the document is a PDF,
  THEN THE SYSTEM SHALL link to `{url}#page={page_start}`; IF `url` is non-null without a page,
  THEN it SHALL link to `{url}`; IF `url` is null, THEN THE SYSTEM SHALL show the source name with
  no link and no broken-link affordance.
- **AC-14** WHILE the citation panel is open, THE SYSTEM SHALL trap focus within it and SHALL
  restore focus to the originating chip on close (Escape, backdrop, or close control).

### 3.3 Sessions & memory

- **AC-15** WHEN the app loads with no session, THE SYSTEM SHALL `POST /api/sessions` with
  `credentials: "include"` and use the returned id for subsequent asks.
- **AC-16** WHEN a follow-up is asked, THE SYSTEM SHALL reuse the same `session_id`, so the thread
  and the server-side memory stay aligned.
- **AC-17** WHEN a `stage` event with id `summarizing_memory` and `status:"started"` arrives, THE
  SYSTEM SHALL render it as "Condensing earlier conversation…".
- **AC-18** WHEN `meta.memory_summarized` is `true`, THE SYSTEM SHALL mark that turn with a quiet
  "ran on a condensed history" note in the expanded timing detail.
- **AC-19** WHILE the user is authenticated, THE SYSTEM SHALL show the session list from
  `GET /api/sessions`, ordered by `last_active_at` descending, each row showing `title` (falling
  back to "Untitled chat") and a relative timestamp.
- **AC-20** WHILE the user is anonymous, THE SYSTEM SHALL show only the current conversation plus a
  "Log in to keep your chats" action, and SHALL NOT call `GET /api/sessions` (it is 401 for anon).
- **AC-21** WHEN a session row is opened, THE SYSTEM SHALL load `GET /api/sessions/{id}/messages`
  and rebuild the thread, rendering stored `citations` as chips and `refused` turns in the refusal
  state.
- **AC-22** WHEN a session is deleted from its row menu, THE SYSTEM SHALL `DELETE /api/sessions/{id}`
  after an inline confirm, remove the row, and start a new chat if the deleted one was open.
- **AC-23** WHEN "New chat" is used, THE SYSTEM SHALL create a fresh session and clear the thread
  without discarding the previous session server-side.

### 3.4 Refusal and failure states

- **AC-24** WHEN `meta.refused` is `true`, THE SYSTEM SHALL render the turn in the refusal state —
  visually distinct from both a normal answer and an error — showing student-facing copy mapped
  from `refusal_reason` and any citations returned as "you might check" suggestions.
  **`refusal_reason` is a machine token, not prose.** Confirmed against a live run: the wire values
  are `low_retrieval_confidence` (`baseline.py:260`) and `no_grounded_claims` (`baseline.py:334`,
  `refusal.py:57`). THE SYSTEM SHALL NOT render the raw token, and SHALL fall back to generic copy
  for an unrecognised one.
- **AC-25** WHEN a response is `429`, THE SYSTEM SHALL read `Retry-After`, disable the composer, and
  count down in place, re-enabling at zero.
- **AC-26** WHEN a response is `503` or a terminal SSE `error` event arrives, THE SYSTEM SHALL keep
  any partial answer already streamed, mark the turn incomplete, and offer "Try again" which re-asks
  the same question in the same session.
- **AC-27** WHEN the stream drops without `done` (network loss or the server timeout `error`), THE
  SYSTEM SHALL behave as AC-26 — partial text is never discarded.
- **AC-28** WHEN a response is `409` with `detail: "session_busy"`, THE SYSTEM SHALL keep the
  composer disabled with "Finishing your last question…" until the in-flight turn ends, then
  re-enable — it SHALL NOT surface this as an error.
- **AC-29** IF `GET /api/health` reports any dependency unhealthy at load, THEN THE SYSTEM SHALL show
  a dismissible banner naming the degradation; it SHALL NOT block asking.
  **The endpoint answers HTTP 503 when degraded**, with the detail in the body — so the client SHALL
  read the body of a failed response rather than discarding it on `!res.ok`. A dependency reported
  as `skipped` (an unconfigured Redis is a valid deployment) SHALL NOT count as degraded.
- **AC-30** THE SYSTEM SHALL parse both error body shapes (§1.5) and SHALL never render a raw JSON
  blob to the user.

### 3.5 Auth

- **AC-31** WHEN login succeeds, THE SYSTEM SHALL hold the access token in memory only, persist the
  refresh token per §4, and fetch `GET /api/auth/me` to establish `role`.
- **AC-32** WHEN any authenticated request returns `401`, THE SYSTEM SHALL attempt `POST
  /api/auth/refresh` once, retry the original request on success, and log out on failure.
- **AC-33** WHILE a refresh is in flight, THE SYSTEM SHALL queue concurrent 401-failed requests
  behind that single refresh — never fire parallel refreshes.
- **AC-34** WHEN logout is used, THE SYSTEM SHALL call `POST /api/auth/logout`, discard both tokens,
  and return to an anonymous chat.
- **AC-35** WHILE the user's `role` is not `admin`, THE SYSTEM SHALL not render the admin route or
  its navigation entry; direct navigation SHALL redirect to the chat. The client guard is
  presentation only — `/internal/*` is admin-gated server-side regardless.
- **AC-36** WHEN registration returns `201`, THE SYSTEM SHALL log the user in with the same
  credentials and land them on the chat.

### 3.6 Language & direction

- **AC-37** THE SYSTEM SHALL set `dir="auto"` on the composer and on every message body so mixed
  Urdu/Latin content resolves per-paragraph.
- **AC-38** THE SYSTEM SHALL load an Urdu-capable fallback (Noto Nastaliq Urdu) subset to
  Arabic-script ranges via `unicode-range`, so a pure-Latin session downloads no Urdu font bytes.
- **AC-39** WHILE an answer streams, THE SYSTEM SHALL NOT re-evaluate direction per token in a way
  that visibly reflows settled text.

### 3.7 Accessibility & performance

- **AC-40** THE SYSTEM SHALL expose the streaming answer in an `aria-live="polite"` region and the
  stage trail in a **separate** `aria-live="polite"` region, so stage changes are announced without
  re-announcing the whole answer.
- **AC-41** THE SYSTEM SHALL submit on Enter and insert a newline on Shift+Enter.
- **AC-42** THE SYSTEM SHALL render a visible focus ring on every interactive element and SHALL meet
  4.5:1 contrast for body text and 3:1 for UI boundaries.
- **AC-43** WHILE `prefers-reduced-motion: reduce` is set, THE SYSTEM SHALL drop the stamp-press,
  trail, and sheet transitions to instant state changes; token streaming itself is content, not
  motion, and is unaffected.
- **AC-44** THE SYSTEM SHALL be usable at a 360px viewport with no horizontal scroll.
- **AC-45** THE SYSTEM SHALL score ≥85 performance and ≥90 accessibility on Lighthouse mobile
  against the mock server.

---

## 4. Decision: token storage and deploy topology

The brief leaves this to the spec. Two shipped facts decide it:

1. `POST /api/auth/token` returns the refresh token **in the JSON body** (`TokenResponse`) — there is
   no httpOnly refresh cookie to adopt. Making one is a backend change (§9).
2. The **anonymous session cookie is `SameSite=Lax`** (`sessions.py:79`). On a split origin
   (`app.vercel.app` → `api.onrender.com`) the browser withholds it, so anonymous multi-turn silently
   degrades to single-turn: `_resolve_owned` sees no cookie and returns 404.

**Decision: deploy same-site.** Vercel rewrites `/api/*` and `/internal/*` to the Render origin, so
the browser only ever sees one origin. This keeps the Lax cookie valid, removes CORS preflight from
the hot path, and needs no backend change.

**Consequence:** access token in memory (lost on reload, by design); refresh token in
`localStorage`. Tradeoff, stated plainly: `localStorage` is readable by any XSS on the origin. It is
accepted here because the app renders **no untrusted HTML** — answers are markdown-lite with an
allowlist and no `dangerouslySetInnerHTML` — and because F10's `refresh_tokens` table is the
blacklist, so a stolen token is revocable. The upgrade path is §9-1.

`CORS_ALLOW_ORIGINS` still needs the Vercel domain for preview deployments, which are cross-origin;
preview builds therefore have known-degraded anonymous memory. That is acceptable for previews and
must be noted in the F15 deploy doc.

---

## 5. Non-functional requirements

- **NFR-1** First contentful paint under 1.8s on a throttled "Slow 4G/3G" profile; the chat shell is
  the initial route and admin/sources/auth routes are lazy chunks.
- **NFR-2** Total font payload ≤ 120KB for a Latin-only session.
- **NFR-3** No runtime dependency outside the fixed stack: React 18, Vite, TypeScript, Tailwind,
  TanStack Query, react-router. SSE uses the native `fetch` reader — **no `EventSource`**, since it
  cannot send a POST body or an `Authorization` header.
- **NFR-4** The typed API client is generated from `/openapi.json`, not hand-written, so an F11
  contract change breaks the build rather than production.

---

## 6. Copy rules

- The system never apologises and never hedges. A refusal states what it searched and did not find.
- Stage labels are plain and in the present participle: "Searching documents", "Reranking results",
  "Condensing earlier conversation".
- One action keeps one name everywhere: the control that says **Ask** produces a turn labelled
  **Asked**; **Delete chat** produces "Chat deleted".
- Errors say what happened and what to do: "Too many questions in a minute. Try again in 24s."
- The empty state is an invitation, not a greeting — six example questions, three English, three
  code-switched, drawn from the real corpus (probation, fee refund, plagiarism, attestation,
  attendance shortage, supplementary exam).
- The disclaimer is permanent and specific: "Answers come from PU and HEC documents and always cite
  them. Check the cited page before you act on it."

---

## 7. Test strategy

- **Unit (Vitest):** SSE frame parser, `[n]` marker resolution, refresh-queue single-flight, error
  body normaliser across both shapes, `Retry-After` countdown.
- **Component (Vitest + Testing Library):** stage trail ordering/collapse, refusal vs error rendering,
  citation panel focus trap, session-busy composer lock.
- **Mock server (MSW):** a scripted SSE fixture per scenario — happy path, refusal, degraded, 429,
  503, mid-stream disconnect, 409 session-busy, `summarizing_memory`, unknown stage id.
- **Lighthouse CI:** mobile profile against the MSW build, thresholds per AC-45.

No pytest changes. No eval gate: F14 is Phase D and changes no retrieval behaviour, so the
`docs/eval_results/` sequence is untouched.

---

## 8. Definition of done

All of §3 pass, §7 suites are green in CI, and the app builds and runs against the real backend with
`VITE_API_BASE_URL` pointed at a local `docker compose` stack.

---

## 9. Out of scope / backend gaps

1. **httpOnly refresh cookie.** Would remove the localStorage tradeoff in §4. Requires
   `auth.py` to set/read a cookie. Not F14.
2. **Session rename.** The brief asks for it; there is **no `PATCH /api/sessions/{id}`**. The UI
   shows the server-assigned `title` read-only and offers delete only.
3. **Session titles.** Whatever `service.create_session` writes is what shows. F14 does not generate
   titles client-side.
4. **PWA manifest / offline.** Stretch, explicitly deferred.
5. **`deep` and `skip_cache` toggles.** Present in the wire contract; the student UI does not expose
   them. Reachable in the admin view only.
6. **`flags_override`.** Admin-only server-side; never sent by this client.
7. **`GET /api/history`.** Superseded by the session list per the brief. Not consumed.
8. **LangGraph / LlamaIndex.** v2 stretch, untouched.
9. **`MEMORY_ANON_MAX_MESSAGES` is dead config.** Declared at `settings.py:199` (default 30) but
   referenced nowhere in `backend/app/`, so no anonymous message cap is actually enforced. F14
   builds no UI state for it. Either wire it up backend-side or delete it — not an F14 decision.
10. **`AnswerResponse` / `Citation` / `StageEvent` are absent from `/openapi.json`.** `/api/ask`
    declares no `response_model` (it returns a `StreamingResponse`), and OpenAPI cannot describe an
    event stream in any case. The SSE payload types are therefore hand-written in
    `frontend/src/api/types.ts` against `app/core/contracts.py`; only the REST shapes are generated.
    Verified field-for-field against a live `meta` frame.
