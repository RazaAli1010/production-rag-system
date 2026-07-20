/**
 * The app's view of the API contract.
 *
 * REST shapes come from `generated.ts` (openapi-typescript, regenerate with `npm run gen:api`).
 * They are re-exported under short names so no component imports the nested
 * `components["schemas"][...]` path.
 *
 * The SSE payload shapes are HAND-WRITTEN, and have to be: OpenAPI cannot describe an event
 * stream, and `/api/ask` declares no `response_model` because it returns a `StreamingResponse` —
 * so `AnswerResponse`, `Citation` and `StageEvent` are absent from the generated schema entirely.
 * Their source of truth is backend/app/core/contracts.py; the drift guard is
 * backend/tests/api/test_openapi.py plus the fixtures in backend/tests/api/conftest.py.
 */

import type { components } from "./generated";

type S = components["schemas"];

export type AskRequest = S["AskRequest"];
export type SessionOut = S["SessionOut"];
export type DocumentOut = S["DocumentOut"];
export type StatsResponse = S["StatsResponse"];
export type TokenResponse = S["TokenResponse"];
export type UserOut = S["UserOut"];
export type UserRole = S["UserRole"];

/**
 * `MessageOut.citations` generates as `unknown[] | null` — the Pydantic model declares a bare
 * `list | None`, so the schema carries no item type. Narrowed here, once.
 */
export type MessageOut = Omit<S["MessageOut"], "citations"> & {
  citations: Citation[] | null;
};

// --- SSE payloads (hand-written; see the module docstring) ------------------------------------

/** Mirrors `app.core.contracts.Citation`. */
export interface Citation {
  chunk_id: string;
  doc_id: string;
  title: string;
  section_heading: string | null;
  page_start: number | null;
  page_end: number | null;
  /** `null` on pre-LLM refusal suggestions, which are built without a `documents.url` lookup. */
  url: string | null;
  /** Always extracted server-side, never LLM-authored. ≤ 25 words. */
  quote: string;
}

/**
 * Mirrors `app.core.contracts.StageEvent`. Note `stage` is a bare `str` on the Python side, NOT a
 * Literal — the union below is the known vocabulary, and the `(string & {})` arm keeps an unknown
 * stage id assignable so a new backend stage degrades to "render the raw id" (AC-4) instead of a
 * type error or a dropped event.
 */
export type StageName =
  | "rewriting"
  | "cache_lookup"
  | "searching"
  | "reranking"
  | "compressing"
  | "generating"
  | "citing"
  | "summarizing_memory"
  | (string & {});

/** One retrieved passage as the trace shows it (`rag/trace.py:chunk_row`) — clipped server-side. */
export interface TraceChunk {
  chunk_id: string;
  title: string;
  section: string | null;
  page: number | null;
  text: string;
  score?: number | null;
  /** Rerank only: old rank − new rank. Positive = the cross-encoder promoted this passage. */
  moved?: number;
}

/**
 * Per-stage intermediate output (`ENABLE_TRACE`), on `done` frames only. Every field is optional
 * because which ones are populated depends on the stage — a `searching` detail has `runs`, a
 * `compressing` detail has `dropped`. Absent entirely when tracing is off, so every consumer must
 * treat it as missing rather than empty.
 */
export interface StageDetail {
  // searching (one entry per query-rewrite fan-out query)
  runs?: { query: string; dense: TraceChunk[]; sparse: TraceChunk[]; fused: TraceChunk[]; degraded: boolean }[];
  // rewriting
  original?: string;
  normalized?: string;
  variants?: string[];
  language?: string | null;
  failed?: boolean;
  // reranking
  before?: TraceChunk[];
  after?: TraceChunk[];
  n_candidates?: number;
  kept?: number;
  // compressing
  tokens_before?: number;
  tokens_after?: number;
  chunks_before?: number;
  chunks_after?: number;
  sentences_dropped?: number;
  dropped?: TraceChunk[];
  trimmed?: { chunk_id: string; title: string; tokens_before: number; tokens_after: number; text_after: string }[];
  // cache_lookup
  hit?: boolean;
  tier?: string;
  key?: string;
  n_entries?: number;
  // generating
  model?: string;
  tokens_out?: number;
  memory_used?: boolean;
  context?: TraceChunk[];
  // summarizing_memory
  pairs_folded?: number;
  summary?: string;
}

export interface StageEvent {
  stage: StageName;
  status: "started" | "done" | "skipped";
  ms: number | null;
  detail?: StageDetail | null;
}

export interface PipelineFlags {
  hybrid: boolean;
  rerank: boolean;
  query_rewrite: boolean;
  compression: boolean;
  cache: boolean;
  memory: boolean;
}

/**
 * The `meta` event payload: `AnswerResponse` minus `answer`
 * (`ask.py` emits `model_dump(exclude={"answer"})`, then stamps `request_id` + `latency_ms`).
 */
export interface AnswerMeta {
  citations: Citation[];
  refused: boolean;
  refusal_reason: string | null;
  pipeline_flags: PipelineFlags;
  session_id: string | null;
  memory_summarized: boolean;
  cache_hit: boolean;
  tokens_in: number;
  tokens_out: number;
  /** True when hybrid retrieval fell back to BM25-only because Pinecone failed. */
  degraded: boolean;
  request_id: string;
  latency_ms: number;
}

/** `GET /api/health` body. Arrives on a 503 when any core dependency is down. */
export interface HealthResponse {
  status: "ok" | "degraded";
  dependencies: Record<string, string>;
}
