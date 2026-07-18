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

export interface StageEvent {
  stage: StageName;
  status: "started" | "done" | "skipped";
  ms: number | null;
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
