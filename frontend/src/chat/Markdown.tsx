import { Fragment } from "react";
import type { Citation } from "../api/types";
import { CitationChip } from "./CitationChip";
// Named `markdownLite`, not `markdown`: a `markdown.ts` beside this `Markdown.tsx` resolves to the
// same module on case-insensitive filesystems (Windows, default macOS), and the import silently
// returns the parser instead of the component.
import { parseBlocks, trimPartialMarker, type Inline } from "./markdownLite";

interface Props {
  text: string;
  citations: Citation[];
  /** While true, a trailing partial `[1` is held back for a frame. */
  streaming?: boolean;
  onOpenCitation: (c: Citation, index: number) => void;
}

function renderInline(
  parts: Inline[],
  citations: Citation[],
  onOpen: Props["onOpenCitation"],
  keyBase: string,
) {
  return parts.map((p, i) => {
    const key = `${keyBase}-${i}`;
    switch (p.type) {
      case "text":
        return <Fragment key={key}>{p.value}</Fragment>;
      case "code":
        return (
          <code key={key} className="rounded bg-paper px-1 py-0.5 font-mono text-[0.9em]">
            {p.value}
          </code>
        );
      case "strong":
        return (
          <strong key={key} className="font-semibold">
            {p.value}
          </strong>
        );
      case "em":
        return <em key={key}>{p.value}</em>;
      case "cite": {
        const c = citations[p.n - 1];
        // AC-8: a marker with no matching citation is plain text, never a dead control.
        if (!c) return <Fragment key={key}>[{p.n}]</Fragment>;
        return <CitationChip key={key} n={p.n} citation={c} onOpen={onOpen} />;
      }
    }
  });
}

export function Markdown({ text, citations, streaming, onOpenCitation }: Props) {
  const source = streaming ? trimPartialMarker(text) : text;
  const blocks = parseBlocks(source);
  return (
    <>
      {blocks.map((b, bi) =>
        b.type === "p" ? (
          <p key={bi} className="mb-3 last:mb-0">
            {renderInline(b.inlines, citations, onOpenCitation, `b${bi}`)}
          </p>
        ) : (
          <ul key={bi} className="mb-3 list-disc space-y-1 pl-5 last:mb-0">
            {b.items.map((item, ii) => (
              <li key={ii}>{renderInline(item, citations, onOpenCitation, `b${bi}i${ii}`)}</li>
            ))}
          </ul>
        ),
      )}
    </>
  );
}
