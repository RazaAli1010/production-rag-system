import { useCallback, useState } from "react";

/**
 * The user's pipeline selection for a conversation (AC-1/4 on the server side: these ride as
 * `flags_override` + `deep` on every ask).
 *
 * Storage is client-side on purpose — no `sessions` column, no migration. The trade-off: a
 * selection does not follow a logged-in user to a second device, which is acceptable because the
 * flags are a per-conversation experiment, not account state.
 */
export interface Flags {
  hybrid: boolean;
  rerank: boolean;
  query_rewrite: boolean;
  compression: boolean;
  cache: boolean;
  memory: boolean;
  /** Not a PipelineFlags member — the server takes it as the top-level `deep` field. */
  deep: boolean;
}

/**
 * These are UI defaults, NOT a mirror of the backend `.env`: once the user picks, the server's
 * `ENABLE_*` values only supply the starting point for callers that send no override at all.
 * `query_rewrite` and `cache` start off because both regressed on their eval gate —
 * see docs/eval_results (rewrite hurt headline hit@5; the cache never hits code-switched queries).
 */
export const DEFAULT_FLAGS: Flags = {
  hybrid: true,
  rerank: true,
  query_rewrite: false,
  compression: true,
  cache: false,
  memory: true,
  deep: false,
};

const KEY = "campusrag.flags";

type Store = Record<string, Flags>;

function readStore(): Store {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Store) : {};
  } catch {
    return {}; // private mode / corrupt entry — defaults are a fine fallback, never a crash
  }
}

function writeStore(next: Store) {
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* storage full or blocked: the in-memory selection still applies to this conversation */
  }
}

/** Only keys we know — a stale entry from an older build must not smuggle in unknown flags, which
 *  the server rejects with 422. */
function sanitise(v: unknown): Flags {
  const src = (v ?? {}) as Partial<Record<keyof Flags, unknown>>;
  const out = { ...DEFAULT_FLAGS };
  for (const k of Object.keys(DEFAULT_FLAGS) as (keyof Flags)[]) {
    if (typeof src[k] === "boolean") out[k] = src[k] as boolean;
  }
  return out;
}

export function useFlags(sessionId: string | null) {
  const [flags, setFlags] = useState<Flags>(() =>
    sessionId ? sanitise(readStore()[sessionId]) : { ...DEFAULT_FLAGS },
  );

  const persist = useCallback((id: string, f: Flags) => {
    const store = readStore();
    store[id] = f;
    writeStore(store);
  }, []);

  const setFlag = useCallback(
    (key: keyof Flags, value: boolean) => {
      setFlags((prev) => {
        const next = { ...prev, [key]: value };
        if (sessionId) persist(sessionId, next);
        return next;
      });
    },
    [persist, sessionId],
  );

  /** The session was only just created by the first ask — bind the current selection to its id so
   *  a reload mid-conversation restores it. */
  const adoptSession = useCallback(
    (id: string) => {
      setFlags((current) => {
        if (!readStore()[id]) persist(id, current);
        return current;
      });
    },
    [persist],
  );

  /** Opening an existing conversation restores what it was asked with. */
  const loadFor = useCallback((id: string | null) => {
    setFlags(id ? sanitise(readStore()[id]) : { ...DEFAULT_FLAGS });
  }, []);

  return { flags, setFlag, adoptSession, loadFor };
}
