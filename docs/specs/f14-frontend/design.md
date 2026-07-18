# F14 — React Frontend · design.md

## 0. What this feature does *not* touch

Stated first, because the spec template asks for things that do not apply to a frontend feature and
silence would read as an omission:

- **New `Settings` keys: none.** F14 adds no backend config. Its own config is two Vite env vars
  (`VITE_API_BASE_URL`, `VITE_ENABLE_MOCKS`) which live in `frontend/.env`, not in the Pydantic
  `Settings` class — that class governs the Python service, and putting a browser build variable in
  it would be a lie about where the value is read. The one existing backend key F14 *depends* on is
  `CORS_ALLOW_ORIGINS`, which F15 populates with the deployed origins.
- **Alembic migrations: none.** No schema change. Every table F14 reads is reached through an
  existing F10/F11/F13/F17 endpoint.
- **LCEL / F3 retriever seam: untouched.** The frontend sits strictly outside the pipeline; the only
  contract it honours is the SSE event order F3 produces and F11 serves. No chain composition here.
- **Eval gate: not applicable.** Phase D. Retrieval behaviour is unchanged, so no F4 run and no new
  file in `docs/eval_results/`.
- **Metrics: nothing new to log.** Every figure the UI displays (`latency_ms`, `cache_hit`,
  `refused`, `degraded`, `tokens_in/out`, `request_id`) is already written to `request_logs` by
  F13's `_emit_request_log` and rendered by `/internal/stats`. F14 adds no client telemetry.

---

## 1. Visual direction

### 1.1 The subject, honestly

The corpus is bureaucratic paper: PU Calendar statutes, HEC notifications, gazette-numbered clauses,
scanned pages with a registrar's stamp in the corner. The product's entire value proposition is that
it behaves like that paper — cites a clause, refuses when the clause is not there. So the interface
borrows the vernacular of **official attestation**, not the vernacular of a chatbot.

The audience corrects the direction away from stuffiness: a nineteen-year-old on a 360px Android
phone on campus wifi. So: the *structure* is official, the *density and speed* are mobile-native.

### 1.2 Tokens

**Colour** — a ruled-register ground with two inks and one stamp.

| Token | Hex | Role |
|---|---|---|
| `--paper` | `#F2F5F8` | app ground — pale blue-grey ruled-register stock, not cream |
| `--paper-raised` | `#FFFFFF` | assistant bubbles, panels, cards |
| `--ink` | `#152A4E` | body text, headings — official navy, never pure black |
| `--ink-muted` | `#5A6B85` | timings, timestamps, counters |
| `--seal` | `#1F6F5C` | primary actions, PU-adjacent institutional green |
| `--stamp` | `#5B3A8E` | **citations only** — violet stamp-pad ink |
| `--flag` | `#B0472B` | refusal state and error state, differentiated by weight not hue |

Dark mode inverts to `--ink` `#0E1725` ground with `--paper` becoming `#E6ECF3` text; `--stamp`
lifts to `#A98BD8` to hold 4.5:1.

**Type** — one superfamily, three roles. IBM Plex, self-hosted, subset.

| Role | Face | Use |
|---|---|---|
| Display | IBM Plex Sans Condensed 700 | wordmark, empty-state headline, section eyebrows |
| Body | IBM Plex Sans 400/600 | messages, controls, everything read |
| Utility | IBM Plex Mono 500 | stage timings, page numbers, doc ids, stats figures |
| Urdu | Noto Nastaliq Urdu 400 | `unicode-range` fallback for Arabic-script ranges only |

The restraint is deliberate and it is a *performance* decision, not timidity: NFR-2 caps Latin fonts
at 120KB on 3G, and a superfamily buys three distinct voices from one set of metrics. The condensed
cut carries the institutional register on its own; a fourth display face would cost more than it
says. Mono for timings and page numbers is not decoration — those are figures the reader compares
across rows, and tabular alignment is what makes them comparable.

Scale: 13 / 15 / 17 / 21 / 32, 1.5 body leading, 1.15 display leading. Body is 17px on mobile — one
notch above the reflexive 16, because this content is read carefully, not skimmed.

**Layout**

```
mobile (360–767)                  desktop (≥1024)
┌──────────────────────┐          ┌────────┬──────────────────┬─────────┐
│ ▤  CampusRAG      ⋯  │          │ chats  │  CampusRAG    ⋯  │ citation│
├──────────────────────┤          │        ├──────────────────┤  panel  │
│                      │          │ + New  │                  │         │
│   thread             │          │ ────── │   thread         │ title   │
│   (user right,       │          │ probn. │                  │ §4.2 p12│
│    assistant left)   │          │ fee re.│                  │ "quote" │
│                      │          │ plagi. │                  │ ─────── │
│  ┌ stamp trail ────┐ │          │        │  ┌ trail ─────┐  │ Open ↗  │
│  │ ▪ Searching…    │ │          │        │  │ ▪ …        │  │         │
│  └─────────────────┘ │          │        │  └────────────┘  │         │
├──────────────────────┤          │        ├──────────────────┤         │
│ [All][PU][HEC]       │          │        │ [All][PU][HEC]   │         │
│ ┌──────────────┐ Ask │          │        │ ┌──────────┐ Ask │         │
│ └──────────────┘ 0/500          │        │ └──────────┘     │         │
│ cites PU & HEC docs  │          └────────┴──────────────────┴─────────┘
└──────────────────────┘          citation panel is a bottom sheet <768
```

The thread column caps at 68ch. The sidebar is a drawer below 1024. The citation panel is a sheet
below 768 and a third column above — it never overlays the thread on desktop, because comparing the
answer against its source side by side *is* the verification act the product exists for.

### 1.3 The signature: the stamp trail

The brief's hero is the live pipeline status, so that is where the boldness goes and nowhere else.

Each stage is a small rectangular **stamp impression** — violet-on-paper, 1px inset border, ~0.6°
rotation alternating in sign down the trail, slightly uneven ink weight. A `started` stage lands with
a 120ms press (scale 1.04 → 1, opacity 0 → 1); on `done` its label gains a mono `180ms` in
`--ink-muted`; a `skipped` stage renders at 40% opacity with a struck label, still legible, because
"we skipped reranking" is information the user is entitled to.

When the first token arrives the trail collapses upward into **one** stamp chip: `worked 2.1s`.
Tapping it re-expands the full trail with per-stage timings. That chip stays on the finished message
permanently — it is the receipt.

Everything else stays quiet: flat bubbles, 6px radius, one hairline divider per structural break, no
gradients, no shadows except the citation sheet's single elevation.

**Structure encodes content, so:** the trail is ordered and connected because the pipeline genuinely
*is* a sequence and the order carries meaning. Nothing else in the UI is numbered — the session list
is reverse-chronological with relative times, and citations use `[n]` because that is the answer's
own reference scheme, not a decorative counter.

### 1.4 Self-critique against the defaults

Checked against the three current AI-design clusters: not cream + high-contrast serif + terracotta;
not near-black + acid accent; not broadsheet hairlines and zero-radius columns. The ruled blue-grey
ground and the violet stamp ink both come from the subject's own materials.

The accessory removed: an earlier pass gave assistant bubbles a torn-paper top edge and a faint
ruled-line background texture. Both were atmosphere, not information — they cost bytes and made
Urdu text harder to read against the rules. Cut. The stamp trail carries the paper metaphor alone,
which is what "spend your boldness in one place" means.

---

## 2. Module layout

```
frontend/
├── index.html
├── vite.config.ts                 # dev proxy /api,/internal → VITE_API_BASE_URL
├── tailwind.config.ts             # §1.2 tokens as the theme, not arbitrary values
├── src/
│   ├── main.tsx  App.tsx  routes.tsx
│   ├── api/
│   │   ├── generated.ts           # openapi-typescript output — DO NOT EDIT
│   │   ├── client.ts              # fetchJson + refresh single-flight
│   │   ├── errors.ts              # normaliseError — both body shapes
│   │   └── sse.ts                 # askStream: POST → ReadableStream → AskEvent
│   ├── auth/
│   │   ├── AuthContext.tsx        # Context + reducer
│   │   └── tokens.ts              # memory access token, localStorage refresh
│   ├── chat/
│   │   ├── useAsk.ts              # the streaming state machine
│   │   ├── Thread.tsx  Message.tsx  Composer.tsx
│   │   ├── StampTrail.tsx  WorkedChip.tsx      # §1.3
│   │   ├── Markdown.tsx           # allowlist renderer + [n] resolution
│   │   ├── CitationPanel.tsx  CitationChip.tsx
│   │   └── RefusalCard.tsx  StreamErrorCard.tsx
│   ├── sessions/  SessionList.tsx  useSessions.ts  Sidebar.tsx
│   ├── pages/     Chat.tsx  Login.tsx  Register.tsx  Sources.tsx  Admin.tsx
│   ├── ui/        Button.tsx  Chip.tsx  Sheet.tsx  Banner.tsx
│   └── mocks/     handlers.ts  sseFixtures.ts     # MSW; scenario per §7 of requirements
└── tests/
```

---

## 3. Key signatures

### 3.1 SSE reader — `api/sse.ts`

`EventSource` is not usable: it is GET-only and cannot carry an `Authorization` header. Native
`fetch` + `ReadableStream` it is.

```ts
export type AskEvent =
  | { event: "stage";     data: StageEvent }
  | { event: "token";     data: { token: string } }
  | { event: "citations"; data: { citations: Citation[] } }
  | { event: "meta";      data: AnswerMeta }          // AnswerResponse minus `answer`
  | { event: "done";      data: unknown }
  | { event: "error";     data: { message: string } };

export async function* askStream(
  body: AskRequest,
  opts: { signal: AbortSignal; accessToken?: string },
): AsyncGenerator<AskEvent>;
```

Two things the parser must get right, both of which are where naive implementations break:

1. **Frames split across chunk boundaries.** Keep a string buffer, split on `\n\n`, retain the
   trailing partial. A `token` frame arriving as two reads must not emit two tokens or drop one.
2. **A 4xx/5xx never becomes a stream.** If `res.ok` is false, read the JSON body and throw a
   normalised `ApiError` before touching the reader — 429/409/503 arrive this way.

`askStream` yields; it holds no state. The state machine is `useAsk`.

### 3.2 The turn state machine — `chat/useAsk.ts`

```ts
type TurnStatus = "idle" | "streaming" | "done" | "refused" | "interrupted" | "rate_limited";

interface Turn {
  id: string;
  question: string;
  answer: string;              // grows token by token
  stages: StageEvent[];        // arrival order, `done` merged onto its `started`
  citations: Citation[];
  meta?: AnswerMeta;
  status: TurnStatus;
  error?: ApiError;
}

export function useAsk(sessionId: string | null): {
  turns: Turn[];
  ask(question: string, namespace?: "pu" | "hec"): Promise<void>;
  retry(turnId: string): Promise<void>;
  busyUntil: number | null;    // epoch ms; drives the 429 countdown and 409 lock
  stop(): void;                // AbortController
};
```

Reducer rules that fall straight out of the contract:

- `stage` with `status:"done"` **merges onto** the matching `started` entry rather than appending, so
  the trail shows five stages, not ten.
- The first `token` sets `trailCollapsed = true` (AC-5).
- `meta.refused` → `status: "refused"`, not `"done"` — refusal is a valid answer, and the card that
  renders it is `RefusalCard`, never `StreamErrorCard` (AC-24).
- A generator that ends without `done`, and a terminal `error` event, both land on
  `status: "interrupted"` with `answer` preserved verbatim (AC-26/27). This is one code path, because
  a mid-stream server timeout and a dropped 3G connection are indistinguishable to the client and
  should be — both mean "you have part of an answer, try again".
- `retry` re-sends the same question with the same `session_id` and replaces the turn in place.

### 3.3 Auth client — `api/client.ts`

```ts
export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T>;
```

Refresh is **single-flight**: a module-level `Promise<string> | null`. Every request that hits 401
awaits the same promise, then retries exactly once. Failure clears both tokens and dispatches
`logout`. Without this, a chat load that fires session-list + me + documents in parallel would burn
three refresh tokens and, since F10 rotates them, invalidate its own session (AC-33).

`credentials: "include"` on every call — the anonymous session cookie depends on it.

### 3.4 Error normalisation — `api/errors.ts`

```ts
export interface ApiError {
  status: number;
  type: string;        // "rate_limited" | "session_busy" | "validation_error" | ...
  message: string;     // already user-facing
  requestId?: string;
  retryAfterS?: number;
}
export async function normaliseError(res: Response): Promise<ApiError>;
```

Handles both shapes from requirements §1.5: `{error:{type,message,request_id}}` from the F11
handlers, and bare `{detail}` from raw `HTTPException`s. For the `{detail}` shape, `type` is derived
— `detail === "session_busy"` → `type: "session_busy"` — which is what lets `useAsk` treat 409 as a
lock rather than an error (AC-28). `Retry-After` is read from the header, not the body.

### 3.5 Markdown and citation resolution — `chat/Markdown.tsx`

Markdown-lite by allowlist: paragraphs, bold, italic, inline code, unordered lists, line breaks.
Nothing else, and **no raw HTML** — `dangerouslySetInnerHTML` appears nowhere in this codebase. That
is what makes the localStorage tradeoff in requirements §4 defensible, so it is a hard rule, not a
preference.

`[n]` is matched after rendering, over text nodes only, so a `[3]` inside a code span stays literal.
Resolution is against `turn.citations[n-1]`; unmatched markers render as plain text (AC-8). During
streaming a trailing partial marker (`[` or `[1`) is held back one frame to avoid a flash of `[1`
becoming a chip and reflowing the line.

---

## 4. Data flow — one ask

```
Composer
  │ question, namespace
  ▼
useAsk.ask ──► ensureSession() ──► POST /api/sessions (once, credentials:include)
  │                                    └─► anon: Set-Cookie (signed, Lax, httpOnly)
  ▼
askStream(body) ──► POST /api/ask  Accept: text/event-stream
  │                     Authorization: Bearer <in-memory>   (if logged in)
  │
  │  ◄── event: stage {summarizing_memory|rewriting|cache_lookup|searching|
  │                    reranking|compressing|generating|citing, started|done|skipped, ms}
  │        └─► StampTrail: press-in, merge `done`, show mono ms
  │  ◄── event: token {token}          ─► first one collapses trail → WorkedChip
  │        └─► Message: append to answer, aria-live polite, autoscroll-if-pinned
  │  ◄── event: citations {citations}  ─► resolve [n] → CitationChip (violet stamp)
  │  ◄── event: meta {…AnswerResponse} ─► refused? degraded? memory_summarized? latency?
  │  ◄── event: done                   ─► status done|refused, composer re-enabled
  ▼
Thread renders the finished turn: answer + chips + WorkedChip receipt
```

Failure branches out of the same call:

```
res 429 ─► ApiError{retryAfterS}     ─► busyUntil = now + s   ─► countdown, composer locked
res 409 ─► ApiError{session_busy}    ─► composer locked, "Finishing your last question…"
res 503 ─► ApiError{provider_unavailable} ─► StreamErrorCard + Try again
event error / stream ends w/o done   ─► status interrupted, partial answer kept, Try again
```

---

## 5. Error handling matrix

| Condition | Detected by | UI | AC |
|---|---|---|---|
| Question <3 or >500 | client, pre-flight | send disabled, counter state | AC-2 |
| 422 from server | `validation_error` envelope | field message under composer | AC-30 |
| 429 | status + `Retry-After` | in-place countdown, composer locked | AC-25 |
| 409 `session_busy` | `{detail}` shape | composer locked, non-error copy | AC-28 |
| 503 | `provider_unavailable` | error card + Try again | AC-26 |
| SSE `error` event | on a 200 stream | partial kept, interrupted, Try again | AC-26/27 |
| Stream ends, no `done` | generator return | same path as above | AC-27 |
| 401 on any REST call | status | single-flight refresh → retry once → logout | AC-32/33 |
| 404 on session | status | drop the stale id, start a new session | AC-22 |
| `meta.refused` | payload, not status | RefusalCard + suggestions | AC-24 |
| `meta.degraded` | payload | inline "keyword index only" note | AC-9 |
| health degraded | `/api/health` | dismissible banner, never blocking | AC-29 |

The load-bearing distinction: **refusal is not an error and a 409 is not an error.** Both are
ordinary states of a working system, and rendering either in the error style would teach students to
distrust a system that is behaving correctly.

---

## 6. Accessibility notes

Two `aria-live="polite"` regions, deliberately separate: the answer body and the stage trail. One
region would re-announce the entire growing answer on every stage change. The trail region announces
label transitions only, never `ms` values — a screen-reader user does not need "180 milliseconds"
read aloud eight times.

The citation sheet is a `role="dialog"` with `aria-modal`, focus trapped on open, focus returned to
the originating chip on close. `prefers-reduced-motion` drops the stamp press, the trail collapse,
and the sheet slide to instant state changes; token append is unaffected, since it is content
arriving rather than motion.

Contrast is checked at build: `--ink` on `--paper` is 12.6:1, `--stamp` on `--paper-raised` is 7.9:1,
`--flag` on `--paper` is 5.2:1.

---

## 7. Build and CI

`openapi-typescript` runs against a locally booted API and writes `src/api/generated.ts`; CI
regenerates and fails on a diff, so an F11 contract change breaks the build rather than production
(NFR-4). Vitest + Testing Library + MSW for the suites in requirements §7. Lighthouse CI runs the
mobile profile against the MSW production build with the AC-45 thresholds as hard gates.

Route-level code splitting: chat is the initial chunk; `Sources`, `Login`, `Register`, and `Admin`
are lazy. Fonts self-hosted, `woff2`, `font-display: swap`, Latin subset preloaded, Nastaliq loaded
only via `unicode-range` so a Latin-only session never fetches it (AC-38, NFR-2).
