/**
 * T5 — the SSE reader (AC-1).
 *
 * `EventSource` is unusable here: it is GET-only and cannot carry an `Authorization` header, and
 * `/api/ask` is a POST with a JSON body. Native fetch + ReadableStream it is.
 *
 * Wire format is fixed by backend/app/api/ask.py `_encode`:
 *     `event: <name>\ndata: <json>\n\n`
 * No `id:`, no `retry:`, no multi-line `data:`. The parser mirrors `parse_sse` in
 * backend/tests/api/conftest.py so both sides agree on the same shape.
 */

import { normaliseError } from "./errors";
import { getAccessToken } from "./tokens";
import type { AnswerMeta, AskRequest, Citation, StageEvent } from "./types";

export type AskEvent =
  | { event: "stage"; data: StageEvent }
  | { event: "token"; data: { token: string } }
  | { event: "citations"; data: { citations: Citation[] } }
  | { event: "meta"; data: AnswerMeta }
  | { event: "done"; data: Record<string, never> }
  | { event: "error"; data: { message: string } };

/** Split a buffer into complete SSE frames, returning the unconsumed tail. */
function drainFrames(buffer: string): { frames: string[]; rest: string } {
  const parts = buffer.split("\n\n");
  // The last element is either "" (buffer ended on a frame boundary) or a partial frame. Either
  // way it is not yet complete, so it goes back into the buffer. This is the whole reason a naive
  // "parse each chunk" implementation drops or duplicates tokens: a frame can arrive split across
  // any two reads, including mid-`data:`.
  const rest = parts.pop() ?? "";
  return { frames: parts, rest };
}

function parseFrame(frame: string): AskEvent | null {
  let event = "";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) data = line.slice(6);
  }
  if (!event) return null;
  try {
    return { event, data: data ? JSON.parse(data) : {} } as AskEvent;
  } catch {
    return null; // a malformed frame is dropped, never fatal to the stream
  }
}

/**
 * Stream one ask. Yields events in arrival order and returns when the server closes the body.
 *
 * A non-2xx response NEVER becomes a stream: 409 (session_busy), 429, 503 and 422 all arrive this
 * way, so the status is checked and thrown as a normalised `ApiError` before the reader is touched.
 * Note that a mid-stream failure is the opposite case — the response already committed as 200, so
 * it arrives as a terminal `error` EVENT rather than a status.
 */
export async function* askStream(
  body: AskRequest,
  opts: { signal?: AbortSignal } = {},
): AsyncGenerator<AskEvent> {
  const headers = new Headers({
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  });
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch("/api/ask", {
    method: "POST",
    credentials: "include",
    headers,
    body: JSON.stringify(body),
    signal: opts.signal,
  });

  if (!res.ok) throw await normaliseError(res);
  if (!res.body) throw { status: 0, type: "no_stream", message: "The server sent no answer." };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // `stream: true` keeps multi-byte characters intact across chunk boundaries — Urdu answers
      // would otherwise show replacement characters wherever a codepoint straddles a read.
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = drainFrames(buffer);
      buffer = rest;
      for (const frame of frames) {
        const ev = parseFrame(frame);
        if (ev) yield ev;
      }
    }
    // Flush anything the server left without a trailing blank line.
    const tail = parseFrame(buffer.trim());
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}

export const __test = { drainFrames, parseFrame };
