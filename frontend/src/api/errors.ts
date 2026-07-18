/**
 * T3 — error normalisation (AC-30).
 *
 * The API speaks two error dialects and a client that knows only one will mis-render the other:
 *
 *   1. F11's registered handlers (422/429/503/504/500) return the envelope
 *      `{"error": {"type", "message", "request_id", "detail"?}}` — see backend/app/core/errors.py.
 *   2. Raw `HTTPException`s bypass those handlers and return FastAPI's default `{"detail": "..."}`.
 *      This is how 403 (flags_override), 404 (session not found) and — the one that matters —
 *      409 `session_busy` arrive.
 *
 * Everything downstream consumes `ApiError` and never sees which dialect it came from.
 */

export interface ApiError {
  status: number;
  /** Machine-readable discriminator. Derived for the `{detail}` dialect. */
  type: string;
  /** Already user-facing: this string is safe to render as-is. */
  message: string;
  requestId?: string;
  /** Seconds, from the `Retry-After` header on a 429. */
  retryAfterS?: number;
  /** Field-level validation detail, present on 422 only. */
  fields?: { loc?: unknown[]; msg?: string; type?: string }[];
}

/** Fallback copy per status, used when the body is missing, empty or unparseable. */
const FALLBACK: Record<number, string> = {
  401: "Your session expired. Log in again.",
  403: "You don't have access to that.",
  404: "That chat is no longer available.",
  409: "Still finishing your last question.",
  422: "Check the question and try again.",
  429: "Too many questions. Wait a moment.",
  500: "Something broke on our side. Try again.",
  503: "The answer service is unavailable right now.",
  504: "That took too long. Try again.",
};

function retryAfter(res: Response): number | undefined {
  const raw = res.headers.get("Retry-After");
  if (!raw) return undefined;
  const n = Number(raw);
  // Retry-After may also be an HTTP date; we only ever send seconds, but never return NaN.
  return Number.isFinite(n) && n >= 0 ? n : undefined;
}

export async function normaliseError(res: Response): Promise<ApiError> {
  const base: ApiError = {
    status: res.status,
    type: "unknown",
    message: FALLBACK[res.status] ?? "Something went wrong. Try again.",
    retryAfterS: retryAfter(res),
  };

  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return base; // empty or non-JSON body — the fallback copy is the whole answer
  }
  if (typeof body !== "object" || body === null) return base;

  // Dialect 1: the F11 envelope.
  const env = (body as { error?: Record<string, unknown> }).error;
  if (env && typeof env === "object") {
    return {
      ...base,
      type: typeof env.type === "string" ? env.type : base.type,
      message: typeof env.message === "string" ? env.message : base.message,
      requestId: typeof env.request_id === "string" ? env.request_id : undefined,
      fields: Array.isArray(env.detail) ? (env.detail as ApiError["fields"]) : undefined,
    };
  }

  // Dialect 2: bare `{detail}`. `detail` is a machine token here ("session_busy"), not prose, so it
  // becomes `type` — that is what lets useAsk treat 409 as a composer lock rather than an error
  // (AC-28) — and the user-facing string stays the fallback copy.
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") {
    return { ...base, type: detail };
  }

  return base;
}

/** True when the failure is the server saying "your previous turn is still running" (AC-28). */
export function isSessionBusy(e: ApiError): boolean {
  return e.status === 409 && e.type === "session_busy";
}
