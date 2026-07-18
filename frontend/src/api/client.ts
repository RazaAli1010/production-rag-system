/**
 * T4 — the fetch wrapper (AC-32, AC-33).
 *
 * Single-flight refresh is a CORRECTNESS requirement here, not an optimisation.
 * `POST /api/auth/refresh` ROTATES the token family and revokes the old one
 * (backend/app/api/auth.py:43-45 — "Rotation revokes the old family"). Two concurrent refreshes
 * therefore mean the second one presents an already-revoked token, fails, and logs the user out.
 * A chat load fires session-list + me + documents in parallel, so this is the common case, not an
 * edge case. Every 401 awaits the SAME promise.
 */

import { normaliseError, type ApiError } from "./errors";
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "./tokens";

/** In-flight refresh, shared by every caller that hit a 401. */
let refreshInFlight: Promise<string | null> | null = null;

/** Set by AuthContext so a failed refresh can tear down app state, not just tokens. */
let onLogout: (() => void) | null = null;

export function setLogoutHandler(fn: (() => void) | null): void {
  onLogout = fn;
}

/** Test seam: reset module state between cases. */
export function __resetClient(): void {
  refreshInFlight = null;
  onLogout = null;
}

async function performRefresh(): Promise<string | null> {
  const refresh = getRefreshToken();
  if (!refresh) return null;
  try {
    const res = await fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { access_token: string; refresh_token: string };
    setTokens(data.access_token, data.refresh_token);
    return data.access_token;
  } catch {
    return null; // network failure is indistinguishable from rejection here; both mean "logged out"
  }
}

/**
 * Refresh the access token, collapsing concurrent callers onto one request.
 * Clearing `refreshInFlight` in `finally` is safe: awaiters already hold the promise reference, so
 * they resolve from it regardless.
 */
export function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = performRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

function authHeaders(extra?: HeadersInit): Headers {
  const h = new Headers(extra);
  const token = getAccessToken();
  if (token) h.set("Authorization", `Bearer ${token}`);
  return h;
}

function logout(): void {
  clearTokens();
  onLogout?.();
}

/**
 * Raw request with auth + one silent-refresh retry. Returns the `Response` untouched so streaming
 * callers (sse.ts) can read the body themselves.
 *
 * `credentials: "include"` on every call — the anonymous session cookie is httpOnly and SameSite=Lax
 * and the whole anonymous multi-turn flow depends on it riding along.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = () =>
    fetch(path, { ...init, credentials: "include", headers: authHeaders(init.headers) });

  let res = await send();
  if (res.status !== 401) return res;

  // 401 → one shared refresh → retry exactly once. Never a second retry: if the refreshed token is
  // also rejected, the problem is not staleness.
  const token = await refreshAccessToken();
  if (!token) {
    logout();
    return res;
  }
  res = await send();
  if (res.status === 401) logout();
  return res;
}

/** JSON request. Throws `ApiError` on any non-2xx. */
export async function fetchJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");
  const res = await apiFetch(path, { ...init, headers });
  if (!res.ok) throw await normaliseError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export type { ApiError };
