/** T3 — error normalisation across both API error dialects (AC-30). */

import { describe, expect, it } from "vitest";
import { isSessionBusy, normaliseError } from "./errors";

const res = (status: number, body?: unknown, headers?: Record<string, string>) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers });

describe("normaliseError", () => {
  it("reads the F11 envelope", async () => {
    const e = await normaliseError(
      res(503, {
        error: {
          type: "provider_unavailable",
          message: "An upstream model provider is temporarily unavailable.",
          request_id: "req-abc",
        },
      }),
    );
    expect(e).toMatchObject({
      status: 503,
      type: "provider_unavailable",
      requestId: "req-abc",
    });
    expect(e.message).toContain("upstream model provider");
  });

  it("takes Retry-After from the header, not the body", async () => {
    const e = await normaliseError(
      res(429, { error: { type: "rate_limited", message: "Too many requests. Slow down." } }, {
        "Retry-After": "24",
      }),
    );
    expect(e.type).toBe("rate_limited");
    expect(e.retryAfterS).toBe(24);
  });

  it("derives a type from the bare {detail} dialect", async () => {
    const e = await normaliseError(res(409, { detail: "session_busy" }));
    expect(e.type).toBe("session_busy");
    expect(isSessionBusy(e)).toBe(true);
    // The machine token must never reach the user.
    expect(e.message).not.toContain("session_busy");
  });

  it("keeps 422 field detail", async () => {
    const e = await normaliseError(
      res(422, {
        error: {
          type: "validation_error",
          message: "Invalid request",
          detail: [{ loc: ["body", "question"], msg: "too short", type: "string_too_short" }],
        },
      }),
    );
    expect(e.fields?.[0]?.msg).toBe("too short");
  });

  it("falls back rather than throwing on an unparseable body", async () => {
    const e = await normaliseError(new Response("<html>502 gateway</html>", { status: 500 }));
    expect(e.status).toBe(500);
    expect(e.type).toBe("unknown");
    expect(e.message).toBeTruthy();
  });

  it("ignores a non-numeric Retry-After instead of yielding NaN", async () => {
    const e = await normaliseError(res(429, { detail: "slow down" }, { "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT" }));
    expect(e.retryAfterS).toBeUndefined();
  });
});
