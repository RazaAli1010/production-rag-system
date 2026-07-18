/**
 * T10 — markdown-lite, by allowlist (AC-7, AC-8).
 *
 * Deliberately a tokeniser and not a markdown library: the ONLY constructs the answer prompt can
 * produce are emphasis, inline code, lists and `[n]` citation markers. Nothing here can emit HTML —
 * `dangerouslySetInnerHTML` appears nowhere in this codebase, and that is precisely what makes the
 * localStorage refresh-token tradeoff in requirements §4 defensible. Keep it that way.
 */

export type Inline =
  | { type: "text"; value: string }
  | { type: "code"; value: string }
  | { type: "strong"; value: string }
  | { type: "em"; value: string }
  | { type: "cite"; n: number };

export type Block =
  | { type: "p"; inlines: Inline[] }
  | { type: "ul"; items: Inline[][] };

// Order matters: code is matched first so a `[2]` inside backticks is consumed as code and never
// becomes a chip.
const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[(\d+)\])/g;

/**
 * While streaming, a trailing `[` or `[1` is a marker that has not finished arriving. Emitting it
 * as text would flash a literal `[1` that reflows the line a frame later when it becomes a chip.
 */
export function trimPartialMarker(text: string): string {
  return text.replace(/\[\d*$/, "");
}

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    const i = m.index;
    if (i > last) out.push({ type: "text", value: text.slice(last, i) });
    if (m[1]) out.push({ type: "code", value: m[1].slice(1, -1) });
    else if (m[2]) out.push({ type: "strong", value: m[2].slice(2, -2) });
    else if (m[3]) out.push({ type: "em", value: m[3].slice(1, -1) });
    else if (m[4]) out.push({ type: "cite", n: Number(m[5]) });
    last = i + m[0].length;
  }
  if (last < text.length) out.push({ type: "text", value: text.slice(last) });
  return out;
}

export function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  for (const raw of text.split(/\n{2,}/)) {
    const chunk = raw.trim();
    if (!chunk) continue;
    const lines = chunk.split("\n");
    if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
      blocks.push({ type: "ul", items: lines.map((l) => parseInline(l.replace(/^\s*[-*]\s+/, ""))) });
    } else {
      blocks.push({ type: "p", inlines: parseInline(lines.join(" ")) });
    }
  }
  return blocks;
}
