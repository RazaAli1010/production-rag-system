# F14 — React Frontend · tasks.md

Ordered. Each task is ≤ ~1h and ends with something runnable. Task IDs map to the AC ids in
`requirements.md` §3.

Build order rationale: the wire layer first (T2–T5), because every screen is a consumer of it and a
wrong SSE parser found on day four costs a rewrite; then the mock server (T6), so every UI task from
T7 onward is testable without a live backend, an OpenAI key, or a seeded corpus.

---

## Phase 1 — Scaffold and contract (T1–T6)

### T1 · Scaffold `frontend/`
Vite + React 18 + TS. Tailwind with the §1.2 design tokens as the theme (`paper`, `ink`, `seal`,
`stamp`, `flag`) — no arbitrary hex values anywhere downstream. react-router with the five routes,
all but `/` lazy. TanStack Query provider. `vite.config.ts` proxies `/api` and `/internal` to
`VITE_API_BASE_URL` in dev, mirroring the same-site production topology from requirements §4.
**Test:** `npm run build` and `npm run dev` both succeed; `/` renders a placeholder.

### T2 · Generate the typed client
`openapi-typescript` against a locally booted API → `src/api/generated.ts`. Add
`npm run gen:api` and a CI step that regenerates and fails on a diff.
**Test:** `AskRequest`, `AnswerResponse`, `Citation`, `StageEvent`, `SessionOut`, `MessageOut`,
`StatsResponse`, `DocumentOut`, `TokenResponse`, `UserOut` are all present and importable.

### T3 · `api/errors.ts` — normalise both error shapes
Per design §3.4. Envelope shape and bare `{detail}` shape; derive `type: "session_busy"` from
`detail === "session_busy"`; read `Retry-After` from the header.
**Test:** unit — envelope 429 yields `retryAfterS` from the header; `{detail:"session_busy"}` yields
`type:"session_busy"`; an unparseable body yields a generic message and never throws. (AC-30)

### T4 · `api/client.ts` — fetch wrapper + single-flight refresh
`credentials:"include"` on every call. Memory access token, `localStorage` refresh token. 401 →
one shared refresh promise → retry once → logout on failure.
**Test:** unit with a mocked fetch — three concurrent 401s trigger exactly **one** `POST
/api/auth/refresh` and three successful retries; a failing refresh clears both tokens once.
(AC-32, AC-33)

### T5 · `api/sse.ts` — the streaming parser
POST + `ReadableStream`, buffered `\n\n` framing, `res.ok === false` throws a normalised `ApiError`
before the reader is touched.
**Test:** unit — a fixture fed in one chunk and the same fixture fed byte-by-byte produce identical
event sequences; a frame split mid-`data:` emits once; a 429 response throws rather than yielding.
(AC-1, AC-25)

### T6 · MSW mock server + SSE fixtures
One scripted fixture per scenario: happy path, refusal, degraded, `summarizing_memory`, unknown
stage id, 429 with `Retry-After`, 409 `session_busy`, 503, mid-stream disconnect (stream ends with no
`done`). Plus REST handlers for sessions, auth, documents, health, stats.
**Test:** a smoke test drives each fixture through `askStream` and asserts the event sequence.

---

## Phase 2 — The chat turn (T7–T13)

### T7 · `useAsk` reducer
Turn state machine per design §3.2 — `done` merges onto `started`, first token collapses the trail,
`meta.refused` → `refused`, no-`done` → `interrupted` with the partial answer preserved.
**Test:** unit against the T6 fixtures — happy path ends `done` with 5 merged stages; refusal ends
`refused` not `done`; disconnect ends `interrupted` with non-empty `answer`. (AC-3, AC-5, AC-24,
AC-26, AC-27)

### T8 · Thread and Message
User right / assistant left, 68ch cap, `dir="auto"` per message body, `aria-live="polite"` on the
streaming answer only.
**Test:** component — an RTL Urdu message renders `dir` resolved right; a mixed Urdu/Latin message
does not force the whole paragraph. (AC-37, AC-40)

### T9 · `StampTrail` and `WorkedChip` — the signature
Per design §1.3: press-in on `started`, mono `ms` on `done`, struck at 40% on `skipped`, collapse to
`worked 2.1s` on the first token, tap to re-expand. Unknown stage ids render their raw id. Separate
`aria-live` region announcing labels only, never `ms`. Motion gated on `prefers-reduced-motion`.
**Test:** component — stages appear in arrival order; the unknown-stage fixture renders the raw id
and keeps streaming; the trail collapses on first token and re-expands on activation; with reduced
motion set, no transition is applied. (AC-3, AC-4, AC-5, AC-40, AC-43)

### T10 · `Markdown` + `[n]` resolution
Allowlist renderer, no `dangerouslySetInnerHTML`. Markers matched over text nodes only; unmatched
markers stay plain text; a trailing partial marker is held one frame.
**Test:** component — `[1]` becomes a chip once `citations` arrives; `[9]` with 3 citations stays
plain text; `[2]` inside inline code stays literal. (AC-7, AC-8)

### T11 · `CitationChip` + `CitationPanel`
Sheet below 768px, side column at ≥1024. Title, section heading, page range, quote. Link rules per
AC-13 including the `url: null` case. `role="dialog"`, focus trapped, focus restored on close.
**Test:** component — a citation with `url` + `page_start` links to `#page=N`; one with `url:null`
renders no link; Escape closes and returns focus to the originating chip. (AC-12, AC-13, AC-14)

### T12 · `Composer`
500-char counter, 3-char floor, send disabled outside the range, Enter submits / Shift+Enter
newlines, `dir="auto"`, namespace chips All/PU/HEC with `namespace` sent only when not All.
**Test:** component — 2 chars keeps send disabled; 501 chars keeps it disabled and marks the counter;
Enter submits; the All chip omits `namespace` from the body. (AC-2, AC-11, AC-41)

### T13 · `RefusalCard` and `StreamErrorCard`
Visually distinct from each other and from a normal answer. Refusal shows `refusal_reason` and
suggestion citations; error keeps partial text and offers Try again, which re-asks in the same
session. Inline `degraded` note.
**Test:** component — the refusal fixture renders `RefusalCard` and never the error style; the
disconnect fixture keeps the partial answer and Try again re-issues with the same `session_id`;
the degraded fixture shows the keyword-index note. (AC-9, AC-24, AC-26)

---

## Phase 3 — Sessions, auth, screens (T14–T20)

### T14 · Session bootstrap
`ensureSession()` — `POST /api/sessions` once with `credentials:"include"`, reuse the id for every
ask, and on 404 drop the stale id and create a fresh one.
**Test:** component — the second ask sends the same `session_id`; a 404'd session triggers exactly
one recreate. (AC-15, AC-16)

### T15 · Composer locks: 429 countdown and 409 session_busy
`busyUntil` drives both. 429 counts down from `Retry-After` and re-enables at zero; 409 shows
"Finishing your last question…" in non-error styling.
**Test:** component with fake timers — a 429 with `Retry-After: 24` shows 24 and re-enables at 0;
409 locks without rendering an error card. (AC-25, AC-28)

### T16 · Sidebar and session list
Drawer below 1024. Authed: `GET /api/sessions` ordered by `last_active_at` desc, `title` falling
back to "Untitled chat", relative times, delete with inline confirm. Anonymous: current chat only
plus "Log in to keep your chats", and **no** `GET /api/sessions` call.
**Test:** component — the anonymous render issues zero session-list requests; delete calls
`DELETE /api/sessions/{id}`, removes the row, and starts a new chat when the open one is deleted.
Rename is absent by design (requirements §9-2). (AC-19, AC-20, AC-22, AC-23)

### T17 · Resume a session
`GET /api/sessions/{id}/messages` rebuilds the thread; stored `citations` render as chips; `refused`
turns render in the refusal state.
**Test:** component — a transcript with one refused assistant turn and one cited turn rebuilds both
correctly. (AC-21)

### T18 · Auth screens and context
Login (`POST /api/auth/token`, form-encoded), register (`201` → auto-login), `GET /api/auth/me` for
`role`, header user menu, logout calling `POST /api/auth/logout`.
**Test:** component — register lands on the chat authenticated; logout clears both tokens and
restores the anonymous sidebar. (AC-31, AC-34, AC-36)

### T19 · Sources page
`GET /api/documents` grouped by `source_org`, showing title, `version_label`, `file_type`, and a link
to `url`.
**Test:** component — renders PU and HEC groups from the fixture; empty corpus renders the empty
state, not a spinner. (US-8)

### T20 · Admin route and stats cards
Route guarded on `role === "admin"`; non-admins get no nav entry and a redirect on direct
navigation. Cards from `GET /internal/stats?window=24h`: request count, p50/p95, cache-hit,
refusal, error, degraded rates, cost, tokens saved, flag usage, session stats. Window picker
`24h`/`7d`. Mono figures per §1.2.
**Test:** component — a student principal cannot reach `/admin` and sees no nav entry; an admin
renders every `StatsResponse` field; a 403 from the API renders the error state rather than empty
cards. (AC-35)

---

## Phase 4 — Polish and gates (T21–T25)

### T21 · Fonts and language
Self-host IBM Plex Sans / Sans Condensed / Mono, `woff2`, Latin subset preloaded. Noto Nastaliq Urdu
loaded only via `unicode-range` over Arabic-script ranges.
**Test:** a Latin-only page load fetches zero Nastaliq bytes (network assertion); total Latin font
payload ≤ 120KB. (AC-38, AC-39, NFR-2)

### T22 · Health banner and boot degradation
`GET /api/health` at load; a dismissible banner naming any unhealthy dependency; never blocks asking.
**Test:** component — the degraded fixture shows the banner and the composer stays enabled. (AC-29)

### T23 · Accessibility pass
Focus rings on everything interactive, contrast checks at the values in design §6, the two separate
live regions verified, reduced-motion honoured throughout.
**Test:** `axe` clean on chat / sources / login / admin; keyboard-only walkthrough completes an ask,
opens a citation, and closes it with focus restored. (AC-14, AC-40, AC-42, AC-43)

### T24 · Mobile and throttled-network pass
360px baseline, no horizontal scroll on any route. Verify streaming and stage ordering on a throttled
3G profile.
**Test:** stages appear in order before the first token and collapse on completion under throttling;
no route scrolls horizontally at 360px. (AC-1, AC-3, AC-5, AC-44)

### T25 · Lighthouse CI gate
Mobile profile against the MSW production build, thresholds as hard CI gates.
**Test:** performance ≥85, accessibility ≥90. (AC-45)

---

## Definition of done

1. Every AC in `requirements.md` §3 (AC-1 … AC-45) has a passing test.
2. Vitest, MSW, `axe`, and Lighthouse CI are green in GitHub Actions.
3. `npm run gen:api` produces no diff against the committed client.
4. The app runs end to end against a real local stack (`docker compose up` + a seeded corpus):
   anonymous ask → stages → streamed cited answer → citation panel → follow-up in the same session;
   login → session list → resume; admin → stats.
5. Requirements §9 gaps are still gaps — no backend file changed by this feature.

### No eval gate

F14 is Phase D. It changes no retrieval, ranking, caching, or generation behaviour, so there is no
before/after to measure: the label sequence ends at `f17-memory-after` and F14 adds nothing to
`docs/eval_results/`. The mandatory-eval-gate rule binds Phase B and C features only.

**Reseed warning for step 4:** the pytest suite truncates `documents` and `chunks` in the shared
database, after which every answer refuses. If the manual end-to-end run shows universal refusals,
reseed the corpus before concluding anything about the frontend.
