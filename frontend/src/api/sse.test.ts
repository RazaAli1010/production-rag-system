/**
 * T5 — SSE framing (AC-1, AC-25).
 *
 * The decisive test is that the SAME payload delivered as one chunk and as individual bytes yields
 * an identical event sequence. A frame can split across any two network reads, including mid-`data:`,
 * and a parser that handles chunks independently will drop or duplicate tokens under exactly the
 * throttled-3G conditions the acceptance criteria call for.
 */

import { describe, expect, it, vi } from "vitest";
import { askStream } from "./sse";
import type { AskEvent } from "./sse";

const WIRE =
  'event: stage\ndata: {"stage":"searching","status":"started","ms":null}\n\n' +
  'event: stage\ndata: {"stage":"searching","status":"done","ms":142}\n\n' +
  'event: token\ndata: {"token":"Probation "}\n\n' +
  'event: token\ndata: {"token":"rules [1] "}\n\n' +
  'event: citations\ndata: {"citations":[]}\n\n' +
  'event: meta\ndata: {"refused":false,"latency_ms":900}\n\n' +
  "event: done\ndata: {}\n\n";

function streamOf(chunks: string[]): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status: 200 });
}

async function collect(chunks: string[]): Promise<AskEvent[]> {
  vi.stubGlobal("fetch", vi.fn(async () => streamOf(chunks)));
  const out: AskEvent[] = [];
  for await (const ev of askStream({ question: "probation se kaise nikalta hoon" } as never)) {
    out.push(ev);
  }
  return out;
}

/** Split a string into single-byte chunks, so every frame straddles a read boundary. */
const bytes = (s: string) => Array.from(new TextEncoder().encode(s)).map((b) => String.fromCharCode(b));

describe("askStream", () => {
  it("parses a whole-payload delivery", async () => {
    const evs = await collect([WIRE]);
    expect(evs.map((e) => e.event)).toEqual([
      "stage",
      "stage",
      "token",
      "token",
      "citations",
      "meta",
      "done",
    ]);
  });

  it("produces an identical sequence when delivered byte by byte", async () => {
    const whole = await collect([WIRE]);
    const split = await collect(bytes(WIRE));
    expect(split).toEqual(whole);
  });

  it("does not duplicate or drop a token split mid-frame", async () => {
    const mid = WIRE.indexOf('"Probation ') + 4;
    const evs = await collect([WIRE.slice(0, mid), WIRE.slice(mid)]);
    const tokens = evs.filter((e) => e.event === "token").map((e) => (e.data as { token: string }).token);
    expect(tokens).toEqual(["Probation ", "rules [1] "]);
  });

  it("keeps multi-byte Urdu intact across a chunk boundary", async () => {
    const wire = 'event: token\ndata: {"token":"سہولت"}\n\n';
    const raw = new TextEncoder().encode(wire);
    const cut = 30; // lands inside a multi-byte codepoint
    const dec = new TextDecoder();
    const evs = await collect([
      dec.decode(raw.slice(0, cut), { stream: true }),
      dec.decode(raw.slice(cut)),
    ]);
    expect((evs[0]?.data as { token: string }).token).toBe("سہولت");
  });

  it("throws a normalised error for 429 instead of yielding a stream", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: { type: "rate_limited", message: "Slow down." } }), {
            status: 429,
            headers: { "Retry-After": "30" },
          }),
      ),
    );
    await expect(async () => {
      for await (const _ of askStream({ question: "hello there" } as never)) void _;
    }).rejects.toMatchObject({ status: 429, type: "rate_limited", retryAfterS: 30 });
  });

  it("throws session_busy for 409", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "session_busy" }), { status: 409 })),
    );
    await expect(async () => {
      for await (const _ of askStream({ question: "hello there" } as never)) void _;
    }).rejects.toMatchObject({ status: 409, type: "session_busy" });
  });

  it("surfaces a mid-stream error event rather than throwing", async () => {
    const evs = await collect([
      'event: token\ndata: {"token":"partial"}\n\n',
      'event: error\ndata: {"message":"request timed out"}\n\n',
    ]);
    expect(evs.at(-1)).toEqual({ event: "error", data: { message: "request timed out" } });
  });

  it("ends without `done` when the stream drops, leaving the caller to detect it", async () => {
    const evs = await collect(['event: token\ndata: {"token":"half an ans"}\n\n']);
    expect(evs.some((e) => e.event === "done")).toBe(false);
    expect(evs).toHaveLength(1);
  });
});
