/**
 * T6 — scripted SSE streams, one per scenario.
 *
 * Shape mirrors `make_fake_astream` / `parse_sse` in backend/tests/api/conftest.py so both sides
 * test against the same wire format: `event: <name>\ndata: <json>\n\n`, ordered
 * stage* -> token* -> citations -> meta -> done|error. The backend fake emits only a `searching`
 * stage; these use the full vocabulary because the stamp trail is what they exist to exercise.
 */

import type { AnswerMeta, Citation, StageEvent } from "../api/types";

export const frame = (event: string, data: unknown) =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

const stage = (s: string, status: StageEvent["status"], ms: number | null = null) =>
  frame("stage", { stage: s, status, ms });

/** One frame per word, matching the backend fake's space-split streaming. */
const tokens = (text: string) =>
  text
    .split(" ")
    .map((w) => frame("token", { token: `${w} ` }))
    .join("");

export const CITATION: Citation = {
  chunk_id: "pu-calendar-2023:41",
  doc_id: "pu-calendar-2023",
  title: "University of the Punjab Calendar, Volume II",
  section_heading: "Probation and Removal from Rolls",
  page_start: 112,
  page_end: 112,
  url: "https://pu.edu.pk/calendar/vol-ii.pdf",
  quote: "A student whose CGPA falls below 2.00 shall be placed on probation for the next semester.",
};

const META: AnswerMeta = {
  citations: [CITATION],
  refused: false,
  refusal_reason: null,
  pipeline_flags: {
    hybrid: true,
    rerank: true,
    query_rewrite: false,
    compression: true,
    cache: true,
    memory: true,
  },
  session_id: "11111111-1111-1111-1111-111111111111",
  memory_summarized: false,
  cache_hit: false,
  tokens_in: 1840,
  tokens_out: 96,
  degraded: false,
  request_id: "req-mock-0001",
  latency_ms: 2140,
};

const ANSWER =
  "You are placed on probation when your CGPA falls below 2.00 [1]. To clear it, raise your " +
  "CGPA to 2.00 or above in the following semester.";

/** The reference stream: five stages, then a cited answer. */
export const happy =
  stage("rewriting", "skipped") +
  stage("cache_lookup", "started") +
  stage("cache_lookup", "done", 12) +
  stage("searching", "started") +
  stage("searching", "done", 380) +
  stage("reranking", "started") +
  stage("reranking", "done", 610) +
  stage("compressing", "started") +
  stage("compressing", "done", 240) +
  stage("generating", "started") +
  tokens(ANSWER) +
  stage("generating", "done", 890) +
  stage("citing", "done", 8) +
  frame("citations", { citations: [CITATION] }) +
  frame("meta", META) +
  frame("done", {});

/** Refusal: a valid answer, not an error. Citations arrive as "you might check" suggestions. */
export const refusal =
  stage("searching", "started") +
  stage("searching", "done", 410) +
  stage("generating", "started") +
  tokens("I could not find this in the PU or HEC documents I have.") +
  stage("generating", "done", 300) +
  frame("citations", { citations: [] }) +
  frame("meta", {
    ...META,
    citations: [{ ...CITATION, url: null, quote: "Fee refund is governed by the schedule below." }],
    refused: true,
    // The real wire value is a machine token, not prose — captured from a live run.
    refusal_reason: "low_retrieval_confidence",
    request_id: "req-mock-refuse",
  }) +
  frame("done", {});

/** Pinecone failed; hybrid retrieval fell back to BM25 only. */
export const degraded =
  stage("searching", "started") +
  stage("searching", "done", 700) +
  tokens("Attendance below 75% bars you from the terminal examination [1].") +
  frame("citations", { citations: [CITATION] }) +
  frame("meta", { ...META, degraded: true, request_id: "req-mock-degraded" }) +
  frame("done", {});

/** The 50k-token threshold was crossed: the window shrank and older pairs were folded up. */
export const summarizing =
  stage("summarizing_memory", "started") +
  stage("summarizing_memory", "done", 1450) +
  stage("searching", "started") +
  stage("searching", "done", 390) +
  tokens("Yes — the same rule applies to the supplementary attempt [1].") +
  frame("citations", { citations: [CITATION] }) +
  frame("meta", { ...META, memory_summarized: true, request_id: "req-mock-summary" }) +
  frame("done", {});

/** A stage id this build has no label for: it must render raw, not crash or vanish (AC-4). */
export const unknownStage =
  stage("consulting_registrar", "started") +
  stage("consulting_registrar", "done", 55) +
  stage("searching", "done", 300) +
  tokens("Answer after an unfamiliar stage [1].") +
  frame("citations", { citations: [CITATION] }) +
  frame("meta", META) +
  frame("done", {});

/** Server-side REQUEST_TIMEOUT_S fired: terminal `error` event on an already-committed 200. */
export const midStreamError =
  stage("searching", "done", 300) +
  stage("generating", "started") +
  tokens("Probation is applied when your CGPA") +
  frame("error", { message: "request timed out" });

/** Connection dropped: no `done`, no `error` — the stream simply ends. */
export const disconnect =
  stage("searching", "done", 300) + stage("generating", "started") + tokens("Fee refunds are issued");

export const fixtures = {
  happy,
  refusal,
  degraded,
  summarizing,
  unknownStage,
  midStreamError,
  disconnect,
} as const;

export type FixtureName = keyof typeof fixtures;
