/** The per-conversation pipeline selection: persistence, restore, and the 422 guard. */

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DEFAULT_FLAGS, useFlags } from "./useFlags";

const A = "aaaaaaaa-0000-0000-0000-000000000000";
const B = "bbbbbbbb-0000-0000-0000-000000000000";

describe("useFlags", () => {
  it("persists a selection under the session and restores it on reload", () => {
    const first = renderHook(() => useFlags(null));
    act(() => first.result.current.setFlag("query_rewrite", true));
    act(() => first.result.current.adoptSession(A));

    // A reload is a fresh hook seeded from storage.
    const reloaded = renderHook(() => useFlags(A));
    expect(reloaded.result.current.flags.query_rewrite).toBe(true);
  });

  it("keeps each conversation's pipeline separate", () => {
    const hook = renderHook(() => useFlags(null));
    act(() => hook.result.current.setFlag("hybrid", false));
    act(() => hook.result.current.adoptSession(A));

    act(() => hook.result.current.loadFor(null)); // new chat → defaults
    expect(hook.result.current.flags.hybrid).toBe(DEFAULT_FLAGS.hybrid);
    act(() => hook.result.current.setFlag("deep", true));
    act(() => hook.result.current.adoptSession(B));

    act(() => hook.result.current.loadFor(A));
    expect(hook.result.current.flags.hybrid).toBe(false);
    expect(hook.result.current.flags.deep).toBe(false);
  });

  it("drops unknown keys from a stale stored entry — the server 422s on them", () => {
    localStorage.setItem(KEY, JSON.stringify({ [A]: { hybrid: false, retired_flag: true } }));
    const { result } = renderHook(() => useFlags(A));
    expect(result.current.flags.hybrid).toBe(false); // known key honoured
    expect(result.current.flags).not.toHaveProperty("retired_flag");
    expect(result.current.flags.rerank).toBe(DEFAULT_FLAGS.rerank); // missing key → default
  });

  it("falls back to defaults instead of throwing on corrupt storage", () => {
    localStorage.setItem(KEY, "not json");
    const { result } = renderHook(() => useFlags(A));
    expect(result.current.flags).toEqual(DEFAULT_FLAGS);
  });

  it("does not overwrite the stored pipeline of a conversation being resumed", () => {
    const hook = renderHook(() => useFlags(null));
    act(() => hook.result.current.setFlag("cache", true));
    act(() => hook.result.current.adoptSession(A));
    // Asking again in the same session must not clobber what is already recorded.
    act(() => hook.result.current.loadFor(A));
    act(() => hook.result.current.adoptSession(A));
    expect(renderHook(() => useFlags(A)).result.current.flags.cache).toBe(true);
  });
});

const KEY = "campusrag.flags";
