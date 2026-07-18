/**
 * Token storage (requirements §4).
 *
 * Access token: memory only. It dies on reload, which is the point — a page refresh costs one
 * silent refresh call, not a persisted credential.
 *
 * Refresh token: localStorage. `POST /api/auth/token` returns it in the JSON body, so there is no
 * httpOnly cookie to adopt without a backend change. The tradeoff is stated plainly in
 * requirements §4: localStorage is readable by any XSS on the origin, accepted here because this
 * app renders no untrusted HTML (the markdown renderer is allowlist-only and
 * `dangerouslySetInnerHTML` appears nowhere), and because F10's `refresh_tokens` table is the
 * blacklist, so a stolen token is revocable.
 */

const REFRESH_KEY = "campusrag.refresh";

let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function getRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_KEY);
  } catch {
    return null; // private-mode Safari and friends
  }
}

export function setTokens(access: string, refresh: string): void {
  accessToken = access;
  try {
    localStorage.setItem(REFRESH_KEY, refresh);
  } catch {
    /* storage unavailable — the session simply won't survive a reload */
  }
}

export function clearTokens(): void {
  accessToken = null;
  try {
    localStorage.removeItem(REFRESH_KEY);
  } catch {
    /* nothing to clear */
  }
}
