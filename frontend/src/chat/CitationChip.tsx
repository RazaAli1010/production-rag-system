import type { Citation } from "../api/types";

interface Props {
  n: number;
  citation: Citation;
  onOpen: (c: Citation, index: number) => void;
}

/**
 * The inline `[n]` marker, rendered as a small stamp impression in violet ink — the one place the
 * palette's `--stamp` is used. The slight rotation is what makes it read as pressed onto the page
 * rather than typeset into it.
 */
export function CitationChip({ n, citation, onOpen }: Props) {
  return (
    <button
      type="button"
      onClick={() => onOpen(citation, n)}
      aria-label={`Source ${n}: ${citation.title}`}
      className="mx-0.5 inline-flex -translate-y-px rotate-[-0.6deg] items-center rounded-[3px]
                 border border-stamp/50 bg-stamp/10 px-1.5 font-mono text-xs font-medium
                 leading-[1.4] text-stamp transition-colors hover:bg-stamp/20"
    >
      {n}
    </button>
  );
}
