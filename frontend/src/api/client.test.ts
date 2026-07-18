/**
 * T4 — single-flight refresh (AC-32, AC-33).
 *
 * The load-bearing test is "three concurrent 401s trigger exactly ONE refresh". `/api/auth/refresh`
 * rotates and revokes the token family, so a second concurrent refresh would present a revoked
 * token and log the user out — the bug surfaces as random logouts, never as an obvious failure.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetClient, apiFetch, setLogoutHandler } from "./client";
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "./tokens";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("apiFetch refresh", () => {
  beforeEach(() => {
    __resetClient();
    clearTokens();
    setTokens("stale-access", "refresh-1");
  });
  afterEach(() => vi.restoreAllMocks());

  it("collapses concurrent 401s onto one refresh call", async () => {
    let refreshCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        refreshCalls += 1;
        return json(200, { access_token: "fresh-access", refresh_token: "refresh-2" });
      }
      // Reject the stale token, accept the fresh one.
      return getAccessToken() === "fresh-access" ? json(200, { ok: true }) : json(401, {});
    });
    vi.stubGlobal("fetch", fetchMock);

    const results = await Promise.all([
      apiFetch("/api/sessions"),
      apiFetch("/api/auth/me"),
      apiFetch("/api/documents"),
    ]);

    expect(refreshCalls).toBe(1);
    expect(results.map((r) => r.status)).toEqual([200, 200, 200]);
    expect(getRefreshToken()).toBe("refresh-2");
  });

  it("logs out once when the refresh is rejected", async () => {
    const onLogout = vi.fn();
    setLogoutHandler(onLogout);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/api/auth/refresh") ? json(401, {}) : json(401, {}),
      ),
    );

    const res = await apiFetch("/api/sessions");

    expect(res.status).toBe(401);
    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });

  it("retries exactly once — a still-401 response after refresh is not retried again", async () => {
    let dataCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/auth/refresh")) {
          return json(200, { access_token: "fresh", refresh_token: "r2" });
        }
        dataCalls += 1;
        return json(401, {});
      }),
    );

    await apiFetch("/api/sessions");

    expect(dataCalls).toBe(2); // original + one retry, never a third
  });

  it("does not attempt a refresh when there is no refresh token", async () => {
    clearTokens();
    let refreshCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/auth/refresh")) refreshCalls += 1;
        return json(401, {});
      }),
    );

    await apiFetch("/api/sessions");

    expect(refreshCalls).toBe(0);
  });

  it("sends credentials on every request so the anonymous session cookie rides along", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => json(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/sessions", { method: "POST" });

    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: "include" });
  });
});
