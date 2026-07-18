/** T6 — every fixture drives askStream end to end and produces the sequence its scenario claims. */

import { describe, expect, it } from "vitest";
import { askStream } from "../api/sse";
import type { AskEvent } from "../api/sse";
import type { AnswerMeta } from "../api/types";

async function run(question: string): Promise<AskEvent[]> {
  const out: AskEvent[] = [];
  for await (const ev of askStream({ question } as never)) out.push(ev);
  return out;
}

const metaOf = (evs: AskEvent[]) => evs.find((e) => e.event === "meta")?.data as AnswerMeta;

describe("SSE fixtures", () => {
  it("happy: stages arrive before the first token, done terminates", async () => {
    const evs = await run("__happy probation");
    const firstToken = evs.findIndex((e) => e.event === "token");
    const stagesBefore = evs.slice(0, firstToken).filter((e) => e.event === "stage");
    expect(stagesBefore.length).toBeGreaterThanOrEqual(5);
    expect(evs.at(-1)?.event).toBe("done");
    expect(metaOf(evs).refused).toBe(false);
  });

  it("refusal: terminates cleanly with refused=true and a reason", async () => {
    const evs = await run("__refusal what is the wifi password");
    const meta = metaOf(evs);
    expect(meta.refused).toBe(true);
    expect(meta.refusal_reason).toBeTruthy();
    expect(evs.at(-1)?.event).toBe("done");
  });

  it("degraded: sets degraded on meta", async () => {
    expect(metaOf(await run("__degraded attendance")).degraded).toBe(true);
  });

  it("summarizing: emits summarizing_memory first and flags memory_summarized", async () => {
    const evs = await run("__summarizing aur agar");
    expect((evs[0]?.data as { stage: string }).stage).toBe("summarizing_memory");
    expect(metaOf(evs).memory_summarized).toBe(true);
  });

  it("unknownStage: an unlabelled stage still streams through", async () => {
    const evs = await run("__unknownStage hello");
    const stages = evs.filter((e) => e.event === "stage").map((e) => (e.data as { stage: string }).stage);
    expect(stages).toContain("consulting_registrar");
    expect(evs.at(-1)?.event).toBe("done");
  });

  it("midStreamError: ends on error with tokens already delivered", async () => {
    const evs = await run("__midStreamError probation");
    expect(evs.some((e) => e.event === "token")).toBe(true);
    expect(evs.at(-1)).toMatchObject({ event: "error" });
  });

  it("disconnect: ends with neither done nor error", async () => {
    const evs = await run("__disconnect fee refund");
    expect(evs.some((e) => e.event === "done" || e.event === "error")).toBe(false);
    expect(evs.some((e) => e.event === "token")).toBe(true);
  });

  it.each([
    ["__429 x", 429, "rate_limited"],
    ["__409 x", 409, "session_busy"],
    ["__503 x", 503, "provider_unavailable"],
  ])("%s throws before streaming", async (q, status, type) => {
    await expect(run(q)).rejects.toMatchObject({ status, type });
  });
});
