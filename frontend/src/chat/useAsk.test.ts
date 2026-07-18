/** T7 — turn state machine against the MSW fixtures (AC-3, AC-5, AC-24, AC-26, AC-27, AC-28). */

import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useAsk } from "./useAsk";

const SESSION = "11111111-1111-1111-1111-111111111111";

const askOnce = async (question: string) => {
  const hook = renderHook(() => useAsk(SESSION));
  await act(async () => {
    await hook.result.current.ask(question);
  });
  return hook;
};

describe("useAsk", () => {
  it("merges done stages onto their started entry rather than appending", async () => {
    const { result } = await askOnce("__happy probation se kaise nikalta hoon");
    const turn = result.current.turns[0]!;
    // The fixture opens 4 stages and closes them, plus a skipped and a bare done.
    expect(turn.stages.filter((s) => s.status === "started")).toHaveLength(0);
    expect(turn.stages.map((s) => s.stage)).toEqual([
      "rewriting",
      "cache_lookup",
      "searching",
      "reranking",
      "compressing",
      "generating",
      "citing",
    ]);
    expect(turn.stages.find((s) => s.stage === "searching")?.ms).toBe(380);
    expect(turn.stages.find((s) => s.stage === "rewriting")?.status).toBe("skipped");
  });

  it("collapses the trail once the first token lands, and ends done", async () => {
    const { result } = await askOnce("__happy probation");
    const turn = result.current.turns[0]!;
    expect(turn.trailCollapsed).toBe(true);
    expect(turn.status).toBe("done");
    expect(turn.answer).toContain("probation");
    expect(turn.citations).toHaveLength(1);
  });

  it("ends a refusal as `refused`, never as an error", async () => {
    const { result } = await askOnce("__refusal wifi password");
    const turn = result.current.turns[0]!;
    expect(turn.status).toBe("refused");
    expect(turn.error).toBeUndefined();
    expect(turn.meta?.refusal_reason).toBeTruthy();
    // Suggestion citations ride on meta even though the `citations` event was empty.
    expect(turn.citations).toHaveLength(1);
  });

  it("keeps the partial answer when the stream errors mid-flight", async () => {
    const { result } = await askOnce("__midStreamError probation");
    const turn = result.current.turns[0]!;
    expect(turn.status).toBe("interrupted");
    expect(turn.answer.length).toBeGreaterThan(0);
    expect(turn.error?.message).toContain("timed out");
  });

  it("treats a stream that ends without `done` exactly like a mid-stream error", async () => {
    const { result } = await askOnce("__disconnect fee refund");
    const turn = result.current.turns[0]!;
    expect(turn.status).toBe("interrupted");
    expect(turn.answer).toContain("Fee refunds");
  });

  it("locks the composer with a countdown on 429", async () => {
    const before = Date.now();
    const { result } = await askOnce("__429 too many");
    expect(result.current.busyReason).toBe("rate_limited");
    // Retry-After: 24 from the handler.
    expect(result.current.busyUntil!).toBeGreaterThan(before + 20_000);
    // The question was never accepted, so no turn is left behind to look like a failure.
    expect(result.current.turns).toHaveLength(0);
  });

  it("locks on 409 session_busy without leaving a failed turn in the thread", async () => {
    const { result } = await askOnce("__409 busy");
    expect(result.current.busyReason).toBe("session_busy");
    expect(result.current.turns).toHaveLength(0);
  });

  it("still surfaces a real 503 as a failed turn with a retry", async () => {
    const { result } = await askOnce("__503 provider down");
    expect(result.current.turns[0]!.status).toBe("failed");
    expect(result.current.turns[0]!.error?.type).toBe("provider_unavailable");
  });

  it("retry replaces the turn in place instead of appending a duplicate", async () => {
    const hook = await askOnce("__disconnect fee refund");
    const id = hook.result.current.turns[0]!.id;
    await act(async () => {
      await hook.result.current.retry(id);
    });
    expect(hook.result.current.turns).toHaveLength(1);
    expect(hook.result.current.turns[0]!.id).toBe(id);
  });

  it("carries degraded through to meta", async () => {
    const { result } = await askOnce("__degraded attendance");
    expect(result.current.turns[0]!.meta?.degraded).toBe(true);
  });

  it("records summarizing_memory as an ordinary stage", async () => {
    const { result } = await askOnce("__summarizing aur agar");
    const turn = result.current.turns[0]!;
    expect(turn.stages[0]?.stage).toBe("summarizing_memory");
    expect(turn.meta?.memory_summarized).toBe(true);
  });

  it("keeps an unknown stage id rather than dropping the event", async () => {
    const { result } = await askOnce("__unknownStage hello");
    await waitFor(() =>
      expect(result.current.turns[0]!.stages.map((s) => s.stage)).toContain("consulting_registrar"),
    );
  });
});
