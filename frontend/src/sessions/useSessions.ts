import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJson } from "../api/client";
import type { ApiError } from "../api/errors";
import type { MessageOut, SessionOut } from "../api/types";
import type { Turn } from "../chat/types";

/**
 * T14 — session bootstrap (AC-15, AC-16, AC-22, AC-23).
 *
 * `POST /api/sessions` is called ONCE and its id reused for every ask, which is what keeps the
 * thread and the server-side memory aligned. For an anonymous user the response also sets the
 * signed httpOnly cookie that authorises every later call on that session — so this must not be
 * fired twice, and `credentials: "include"` (handled in client.ts) is non-negotiable.
 */
export function useSession() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const creating = useRef<Promise<string> | null>(null);

  // Bumped whenever the caller selects a session explicitly. A create that was already in flight
  // when that happens must NOT overwrite the selection — otherwise opening a chat while a create
  // settles silently swaps the id back, and the next question is posted to the wrong session.
  const selection = useRef(0);

  const selectSession = useCallback((id: string | null) => {
    selection.current += 1;
    creating.current = null;
    setSessionId(id);
  }, []);

  const ensureSession = useCallback(async (): Promise<string> => {
    if (sessionId) return sessionId;
    if (!creating.current) {
      const startedAt = selection.current;
      creating.current = fetchJson<SessionOut>("/api/sessions", { method: "POST" })
        .then((s) => {
          if (selection.current === startedAt) setSessionId(s.id);
          return s.id;
        })
        .finally(() => {
          creating.current = null;
        });
    }
    return creating.current;
  }, [sessionId]);

  /** AC-22/AC-23: a new chat gets a new server session; the old one is left intact. */
  const newSession = useCallback(async () => {
    selectSession(null);
  }, [selectSession]);

  // `setSessionId` is exposed as `selectSession` so every explicit selection goes through the
  // in-flight guard — a raw setter would reintroduce the race.
  return { sessionId, setSessionId: selectSession, ensureSession, newSession };
}

/** The sidebar list. Authed only — `GET /api/sessions` is 401 for anonymous callers (AC-20). */
export function useSessionList(enabled: boolean) {
  const [sessions, setSessions] = useState<SessionOut[]>([]);
  const [error, setError] = useState<ApiError | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) {
      setSessions([]);
      return;
    }
    try {
      const rows = await fetchJson<SessionOut[]>("/api/sessions");
      // Most recently used first — the one you want is almost always the one you just left.
      setSessions(
        [...rows].sort((a, b) => Date.parse(b.last_active_at) - Date.parse(a.last_active_at)),
      );
    } catch (e) {
      setError(e as ApiError);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const remove = useCallback(
    async (id: string) => {
      await fetchJson<void>(`/api/sessions/${id}`, { method: "DELETE" });
      setSessions((s) => s.filter((r) => r.id !== id));
    },
    [],
  );

  return { sessions, error, refresh, remove };
}

/** AC-21 — rebuild a thread from the stored transcript. */
export function messagesToTurns(msgs: MessageOut[]): Turn[] {
  const turns: Turn[] = [];
  for (const m of msgs) {
    if (m.role === "user") {
      turns.push({
        id: m.id,
        question: m.content,
        answer: "",
        stages: [],
        citations: [],
        status: "done",
        trailCollapsed: true,
      });
      continue;
    }
    if (m.role !== "assistant") continue;
    const open = turns[turns.length - 1];
    // An assistant message with no preceding user turn shouldn't happen, but a transcript is data
    // from the server — render it rather than dropping it.
    if (!open) {
      turns.push({
        id: m.id,
        question: "",
        answer: m.content,
        stages: [],
        citations: m.citations ?? [],
        status: m.refused ? "refused" : "done",
        trailCollapsed: true,
      });
      continue;
    }
    open.answer = m.content;
    open.citations = m.citations ?? [];
    open.status = m.refused ? "refused" : "done";
  }
  return turns;
}

export async function loadTranscript(sessionId: string): Promise<Turn[]> {
  return messagesToTurns(await fetchJson<MessageOut[]>(`/api/sessions/${sessionId}/messages`));
}
