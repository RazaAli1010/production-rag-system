/** T10 — markdown-lite tokenising and `[n]` handling (AC-7, AC-8). */

import { describe, expect, it } from "vitest";
import { parseBlocks, parseInline, trimPartialMarker } from "./markdownLite";

describe("parseInline", () => {
  it("extracts citation markers", () => {
    expect(parseInline("Probation applies [1] always.")).toEqual([
      { type: "text", value: "Probation applies " },
      { type: "cite", n: 1 },
      { type: "text", value: " always." },
    ]);
  });

  it("leaves a marker inside inline code literal", () => {
    const parts = parseInline("use `arr[2]` here");
    expect(parts).toContainEqual({ type: "code", value: "arr[2]" });
    expect(parts.some((p) => p.type === "cite")).toBe(false);
  });

  it("handles bold and italic", () => {
    expect(parseInline("**must** be *2.00*")).toEqual([
      { type: "strong", value: "must" },
      { type: "text", value: " be " },
      { type: "em", value: "2.00" },
    ]);
  });

  it("emits no markup for plain Urdu text", () => {
    expect(parseInline("پروبیشن کے قواعد")).toEqual([{ type: "text", value: "پروبیشن کے قواعد" }]);
  });
});

describe("trimPartialMarker", () => {
  it.each([
    ["Answer so far [", "Answer so far "],
    ["Answer so far [1", "Answer so far "],
    ["Answer so far [12", "Answer so far "],
  ])("holds back %j while streaming", (input, expected) => {
    expect(trimPartialMarker(input)).toBe(expected);
  });

  it("leaves a completed marker alone", () => {
    expect(trimPartialMarker("Answer [1]")).toBe("Answer [1]");
  });
});

describe("parseBlocks", () => {
  it("splits paragraphs and bullet lists", () => {
    const blocks = parseBlocks("First para.\n\n- one\n- two [1]");
    expect(blocks[0]).toMatchObject({ type: "p" });
    expect(blocks[1]).toMatchObject({ type: "ul" });
    expect((blocks[1] as { items: unknown[] }).items).toHaveLength(2);
  });
});
