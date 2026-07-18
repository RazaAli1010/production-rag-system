/**
 * T7 — the turn state machine (AC-3, AC-5, AC-24, AC-26, AC-27, AC-28).
 *
 * Every rule here falls straight out of the wire contract:
 *  - a `done` stage MERGES onto its `started` entry, so the trail shows five stages, not ten;
 *  - the first token collapses the trail into the receipt chip;
 *  - `meta.refused` ends the turn as `refused`, never as an error;
 *  - a stream that ends without `done`, and a terminal `error` event, are ONE code path —
 *    to the client a server timeout and a dropped 3G connection are indistinguishable, and both
 *    mean the same thing to the user: "you have part of an answer, try again".
 */

import { useCallback, useReducer, useRef } from "react";
import type { ApiError } from "../api/errors";
import { isSessionBusy } from "../api/errors";
import { askStream } from "../api/sse";
import type { AnswerMeta, Citation, StageEvent } from "../api/types";
import type { TrailStage, Turn } from "./types";

interface State {
  turns: Turn[];
  /** Epoch ms until which the composer stays locked (429 countdown, 409 lock). */
  busyUntil: number | null;
  busyReason: "rate_limited" | "session_busy" | null;
}

type Action =
  | { t: "start"; id: string; question: string; namespace?: "pu" | "hec" }
  | { t: "stage"; id: string; stage: StageEvent }
  | { t: "token"; id: string; token: string }
  | { t: "citations"; id: string; citations: Citation[] }
  | { t: "meta"; id: string; meta: AnswerMeta }
  | { t: "settle"; id: string; status: Turn["status"]; error?: ApiError }
  | { t: "fail"; id: string; error: ApiError }
  | { t: "drop"; id: string }
  | { t: "lock"; until: number; reason: State["busyReason"] }
  | { t: "unlock" }
  | { t: "load"; turns: Turn[] }
  | { t: "reset" };

/** Merge a `done`/`skipped` onto the matching open `started`, else append. */
function mergeStage(stages: TrailStage[], ev: StageEvent): TrailStage[] {
  if (ev.status === "started") return [...stages, { stage: ev.stage, status: "started", ms: null }];
  const i = stages.findIndex((s) => s.stage === ev.stage && s.status === "started");
  if (i === -1) return [...stages, { stage: ev.stage, status: ev.status, ms: ev.ms }];
  const next = [...stages];
  next[i] = { stage: ev.stage, status: ev.status, ms: ev.ms };
  return next;
}

function patch(state: State, id: string, fn: (t: Turn) => Turn): State {
  return { ...state, turns: state.turns.map((t) => (t.id === id ? fn(t) : t)) };
}

function reducer(state: State, a: Action): State {
  switch (a.t) {
    case "start": {
      const turn: Turn = {
        id: a.id,
        question: a.question,
        answer: "",
        stages: [],
        citations: [],
        status: "streaming",
        trailCollapsed: false,
        namespace: a.namespace,
      };
      // A retry replaces its turn in place rather than appending a duplicate.
      const exists = state.turns.some((t) => t.id === a.id);
      return {
        ...state,
        busyUntil: null,
        busyReason: null,
        turns: exists ? state.turns.map((t) => (t.id === a.id ? turn : t)) : [...state.turns, turn],
      };
    }
    case "stage":
      return patch(state, a.id, (t) => ({ ...t, stages: mergeStage(t.stages, a.stage) }));
    case "token":
      return patch(state, a.id, (t) => ({
        ...t,
        answer: t.answer + a.token,
        trailCollapsed: true, // AC-5: the first token is what collapses the trail
      }));
    case "citations":
      return patch(state, a.id, (t) => ({ ...t, citations: a.citations }));
    case "meta":
      return patch(state, a.id, (t) => ({
        ...t,
        meta: a.meta,
        citations: a.meta.citations.length ? a.meta.citations : t.citations,
      }));
    case "settle":
      return patch(state, a.id, (t) => ({ ...t, status: a.status, error: a.error }));
    case "fail":
      // Pre-stream failure: no partial answer exists, so the turn carries only the error.
      return patch(state, a.id, (t) => ({ ...t, status: "failed", error: a.error }));
    case "drop":
      return { ...state, turns: state.turns.filter((t) => t.id !== a.id) };
    case "lock":
      return { ...state, busyUntil: a.until, busyReason: a.reason };
    case "unlock":
      return { ...state, busyUntil: null, busyReason: null };
    case "load":
      return { ...state, turns: a.turns };
    case "reset":
      return { turns: [], busyUntil: null, busyReason: null };
  }
}

/**
 * ponytail: a 409 means ANOTHER request holds the per-session lock — possibly in another tab, so
 * this client cannot observe when it ends. A short timed lock re-enables the composer on its own
 * instead of stranding the user. Swap for a server-sent "turn finished" signal only if the backend
 * ever grows one.
 */
const SESSION_BUSY_LOCK_MS = 3000;

export function useAsk(sessionId: string | null) {
  const [state, dispatch] = useReducer(reducer, {
    turns: [],
    busyUntil: null,
    busyReason: null,
  });
  const abortRef = useRef<AbortController | null>(null);

  const run = useCallback(
    async (turnId: string, question: string, namespace?: "pu" | "hec") => {
      dispatch({ t: "start", id: turnId, question, namespace });
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;

      let sawDone = false;
      let sawStreamError = false;
      let refused = false;
      try {
        for await (const ev of askStream(
          { question, session_id: sessionId, namespace: namespace ?? null } as never,
          { signal: ctrl.signal },
        )) {
          switch (ev.event) {
            case "stage":
              dispatch({ t: "stage", id: turnId, stage: ev.data });
              break;
            case "token":
              dispatch({ t: "token", id: turnId, token: ev.data.token });
              break;
            case "citations":
              dispatch({ t: "citations", id: turnId, citations: ev.data.citations });
              break;
            case "meta":
              refused = ev.data.refused;
              dispatch({ t: "meta", id: turnId, meta: ev.data });
              break;
            case "done":
              sawDone = true;
              break;
            case "error":
              sawStreamError = true;
              dispatch({
                t: "settle",
                id: turnId,
                status: "interrupted",
                error: { status: 200, type: "stream_error", message: ev.data.message },
              });
              break;
          }
        }
      } catch (err) {
        const e = err as ApiError;
        if (ctrl.signal.aborted) return;
        // 409 and 429 mean the question was never accepted — nothing ran, nothing partial exists.
        // The composer lock and its note ARE the feedback; leaving a failed turn in the thread
        // would read as "your question broke something" for what is really "not yet" (AC-28).
        if (isSessionBusy(e)) {
          dispatch({ t: "lock", until: Date.now() + SESSION_BUSY_LOCK_MS, reason: "session_busy" });
          dispatch({ t: "drop", id: turnId });
          return;
        }
        if (e.status === 429) {
          dispatch({
            t: "lock",
            until: Date.now() + (e.retryAfterS ?? 30) * 1000,
            reason: "rate_limited",
          });
          dispatch({ t: "drop", id: turnId });
          return;
        }
        dispatch({ t: "fail", id: turnId, error: e });
        return;
      }

      if (sawStreamError) return; // already settled as interrupted
      if (!sawDone) {
        // The stream ended without terminating. Same user-visible outcome as an `error` event.
        dispatch({
          t: "settle",
          id: turnId,
          status: "interrupted",
          error: { status: 0, type: "disconnected", message: "The answer stopped partway." },
        });
        return;
      }
      dispatch({ t: "settle", id: turnId, status: refused ? "refused" : "done" });
    },
    [sessionId],
  );

  const ask = useCallback(
    (question: string, namespace?: "pu" | "hec") =>
      run(`turn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, question, namespace),
    [run],
  );

  const retry = useCallback(
    (turnId: string) => {
      const turn = state.turns.find((t) => t.id === turnId);
      if (!turn) return Promise.resolve();
      return run(turnId, turn.question, turn.namespace);
    },
    [run, state.turns],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);
  const reset = useCallback(() => {
    abortRef.current?.abort();
    dispatch({ t: "reset" });
  }, []);
  const load = useCallback((turns: Turn[]) => dispatch({ t: "load", turns }), []);
  const clearLock = useCallback(() => dispatch({ t: "unlock" }), []);

  const streaming = state.turns.some((t) => t.status === "streaming");

  return {
    turns: state.turns,
    busyUntil: state.busyUntil,
    busyReason: state.busyReason,
    streaming,
    ask,
    retry,
    stop,
    reset,
    load,
    clearLock,
  };
}

export const __test = { reducer, mergeStage };
