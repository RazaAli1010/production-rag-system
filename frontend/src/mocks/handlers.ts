/**
 * T6 — MSW request handlers.
 *
 * The ask handler picks its fixture from the question text (`__<name>` prefix) so a component test
 * selects a scenario by asking for it, and the dev server with VITE_ENABLE_MOCKS=true stays usable
 * by hand. Streams are emitted chunk-by-chunk with a small delay so the UI genuinely streams rather
 * than receiving one blob.
 */

import { HttpResponse, delay, http } from "msw";
import { fixtures, type FixtureName } from "./sseFixtures";

const TOKEN_DELAY_MS = 12;

function pickFixture(question: string): string {
  const m = /^__(\w+)/.exec(question.trim());
  const name = m?.[1] as FixtureName | undefined;
  return (name && fixtures[name]) || fixtures.happy;
}

/** Emit one SSE frame at a time so the client exercises its buffering. */
function streamFrames(payload: string): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  const frames = payload.split("\n\n").filter(Boolean).map((f) => `${f}\n\n`);
  return new ReadableStream({
    async start(controller) {
      for (const f of frames) {
        controller.enqueue(enc.encode(f));
        await delay(TOKEN_DELAY_MS);
      }
      controller.close();
    },
  });
}

const SESSION_ID = "11111111-1111-1111-1111-111111111111";
/** A session that exists but holds no messages — what an empty "Untitled chat" row opens to. */
export const EMPTY_SESSION_ID = "22222222-2222-2222-2222-222222222222";
/** Listed but 404s on fetch (deleted in another tab, or not owned by this caller). */
export const MISSING_SESSION_ID = "33333333-3333-3333-3333-333333333333";

export const handlers = [
  http.post("/api/ask", async ({ request }) => {
    const body = (await request.json()) as { question: string };
    const q = body.question ?? "";

    // Status-code scenarios never become a stream.
    if (q.startsWith("__429")) {
      return HttpResponse.json(
        { error: { type: "rate_limited", message: "Too many questions. Slow down." } },
        { status: 429, headers: { "Retry-After": "24" } },
      );
    }
    if (q.startsWith("__409")) {
      return HttpResponse.json({ detail: "session_busy" }, { status: 409 });
    }
    if (q.startsWith("__503")) {
      return HttpResponse.json(
        {
          error: {
            type: "provider_unavailable",
            message: "An upstream model provider is temporarily unavailable.",
          },
        },
        { status: 503 },
      );
    }

    return new HttpResponse(streamFrames(pickFixture(q)), {
      headers: { "Content-Type": "text/event-stream" },
    });
  }),

  http.post("/api/sessions", () =>
    HttpResponse.json(
      {
        id: SESSION_ID,
        title: null,
        total_tokens: 0,
        created_at: new Date().toISOString(),
        last_active_at: new Date().toISOString(),
      },
      { status: 201 },
    ),
  ),

  http.get("/api/sessions", () =>
    HttpResponse.json([
      {
        id: SESSION_ID,
        title: "Probation and CGPA",
        total_tokens: 4200,
        created_at: "2026-07-11T09:00:00Z",
        last_active_at: "2026-07-17T18:20:00Z",
      },
      {
        id: EMPTY_SESSION_ID,
        title: null,
        total_tokens: 900,
        created_at: "2026-07-02T11:00:00Z",
        last_active_at: "2026-07-02T11:40:00Z",
      },
      {
        id: MISSING_SESSION_ID,
        title: "Deleted elsewhere",
        total_tokens: 100,
        created_at: "2026-07-01T09:00:00Z",
        last_active_at: "2026-07-01T09:10:00Z",
      },
    ]),
  ),

  // Keyed on :id. It used to return the same populated transcript for EVERY id, which made the
  // empty-session and 404 paths untestable — the exact bug class that shipped.
  http.get("/api/sessions/:id/messages", ({ params }) => {
    if (params.id === EMPTY_SESSION_ID) return HttpResponse.json([]);
    if (params.id === MISSING_SESSION_ID) {
      return HttpResponse.json({ detail: "Session not found" }, { status: 404 });
    }
    return HttpResponse.json([
      {
        id: "aaaa1111-0000-0000-0000-000000000001",
        role: "user",
        content: "probation se kaise nikalta hoon",
        refused: false,
        citations: null,
        created_at: "2026-07-17T18:19:00Z",
      },
      {
        id: "aaaa1111-0000-0000-0000-000000000002",
        role: "assistant",
        content: "Raise your CGPA to 2.00 or above in the following semester [1].",
        refused: false,
        citations: [
          {
            chunk_id: "pu-calendar-2023:41",
            doc_id: "pu-calendar-2023",
            title: "University of the Punjab Calendar, Volume II",
            section_heading: "Probation and Removal from Rolls",
            page_start: 112,
            page_end: 112,
            url: "https://pu.edu.pk/calendar/vol-ii.pdf",
            quote: "A student whose CGPA falls below 2.00 shall be placed on probation.",
          },
        ],
        created_at: "2026-07-17T18:19:06Z",
      },
    ]);
  }),

  http.delete("/api/sessions/:id", () => new HttpResponse(null, { status: 204 })),

  http.post("/api/auth/token", () =>
    HttpResponse.json({
      access_token: "mock-access",
      refresh_token: "mock-refresh",
      token_type: "bearer",
    }),
  ),

  http.post("/api/auth/refresh", () =>
    HttpResponse.json({
      access_token: "mock-access-2",
      refresh_token: "mock-refresh-2",
      token_type: "bearer",
    }),
  ),

  http.post("/api/auth/register", () =>
    HttpResponse.json(
      {
        id: "99999999-9999-9999-9999-999999999999",
        email: "student@pucit.edu.pk",
        role: "student",
        is_active: true,
        created_at: new Date().toISOString(),
      },
      { status: 201 },
    ),
  ),

  http.post("/api/auth/logout", () => new HttpResponse(null, { status: 204 })),

  http.get("/api/auth/me", () =>
    HttpResponse.json({
      id: "99999999-9999-9999-9999-999999999999",
      email: "student@pucit.edu.pk",
      role: "student",
      is_active: true,
      created_at: "2026-01-04T10:00:00Z",
    }),
  ),

  http.get("/api/documents", () =>
    HttpResponse.json([
      {
        doc_id: "pu-calendar-2023",
        title: "University of the Punjab Calendar, Volume II",
        source_org: "PU",
        version_label: "2023",
        file_type: "pdf",
        url: "https://pu.edu.pk/calendar/vol-ii.pdf",
        status: "indexed",
      },
      {
        doc_id: "hec-plagiarism-policy-2021",
        title: "HEC Plagiarism Policy",
        source_org: "HEC",
        version_label: "2021",
        file_type: "pdf",
        url: "https://hec.gov.pk/plagiarism-policy.pdf",
        status: "indexed",
      },
    ]),
  ),

  http.get("/api/health", () =>
    HttpResponse.json({
      status: "ok",
      dependencies: {
        postgres: "ok",
        redis: "ok",
        pinecone: "ok",
        bm25: "ok",
        openai_key: "ok",
      },
    }),
  ),

  http.get("/internal/stats", () =>
    HttpResponse.json({
      window: "24h",
      request_count: 1284,
      p50_ms: 1900,
      p95_ms: 4300,
      cache_hit_rate: 0.31,
      refusal_rate: 0.12,
      error_rate: 0.004,
      degraded_rate: 0.01,
      total_cost_usd: 1.87,
      tokens_saved_by_cache: 412_000,
      flag_usage: { hybrid: 1284, rerank: 1284, compression: 1180, cache: 400, memory: 1284 },
      top_query_clusters: [
        { cluster: "probation / CGPA", count: 214 },
        { cluster: "fee refund", count: 168 },
      ],
      active_sessions: 96,
      mean_turns_per_session: 3.4,
      summarization_count: 22,
      tokens_saved_by_summarization_est: 13_200,
    }),
  ),
];
