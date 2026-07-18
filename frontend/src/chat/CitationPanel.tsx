import { useEffect, useRef } from "react";
import type { Citation } from "../api/types";

interface Props {
  citation: Citation | null;
  index: number;
  onClose: () => void;
}

/** `#page=N` is the PDF viewer fragment; without a page there is nothing to deep-link to. */
export function sourceHref(c: Citation): string | null {
  if (!c.url) return null;
  return c.page_start ? `${c.url}#page=${c.page_start}` : c.url;
}

export function pageLabel(c: Citation): string | null {
  if (c.page_start == null) return null;
  if (c.page_end != null && c.page_end !== c.page_start) return `Pages ${c.page_start}–${c.page_end}`;
  return `Page ${c.page_start}`;
}

/**
 * T11 — bottom sheet below 768px, side column at ≥1024 (AC-12, AC-13, AC-14).
 *
 * On desktop it sits beside the thread rather than over it: comparing the answer against its source
 * side by side IS the verification act this product exists for, so covering the answer to show the
 * citation would defeat the point.
 */
export function CitationPanel({ citation, index, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const openerRef = useRef<Element | null>(null);

  useEffect(() => {
    if (!citation) return;
    openerRef.current = document.activeElement;
    ref.current?.querySelector<HTMLElement>("button, a")?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab" || !ref.current) return;
      // Focus trap: the sheet is modal on mobile, and letting Tab escape behind the backdrop
      // strands keyboard users on controls they cannot see.
      const items = ref.current.querySelectorAll<HTMLElement>(
        'button, a[href], [tabindex]:not([tabindex="-1"])',
      );
      if (!items.length) return;
      const first = items[0]!;
      const last = items[items.length - 1]!;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      (openerRef.current as HTMLElement | null)?.focus?.();
    };
  }, [citation, onClose]);

  if (!citation) return null;
  const href = sourceHref(citation);
  const pages = pageLabel(citation);

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-ink/30 lg:hidden"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="citation-title"
        className="fixed inset-x-0 bottom-0 z-50 max-h-[75vh] overflow-y-auto rounded-t-xl border-t
                   border-rule bg-paper-raised p-5 shadow-2xl
                   lg:static lg:z-auto lg:max-h-none lg:w-80 lg:shrink-0 lg:rounded-none
                   lg:border-l lg:border-t-0 lg:shadow-none"
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <span className="inline-flex rotate-[-0.6deg] items-center rounded-[3px] border border-stamp/50 bg-stamp/10 px-2 py-0.5 font-mono text-xs font-medium text-stamp">
            Source {index}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="-mr-1 -mt-1 rounded px-2 py-1 text-sm text-ink-muted hover:text-ink"
          >
            Close
          </button>
        </div>

        <h2 id="citation-title" className="font-display text-lg font-bold leading-tight">
          {citation.title}
        </h2>

        {citation.section_heading && (
          <p className="mt-1 text-sm text-ink-muted">{citation.section_heading}</p>
        )}
        {pages && <p className="mt-1 font-mono text-xs text-ink-muted">{pages}</p>}

        <blockquote className="my-4 border-l-2 border-stamp/60 pl-3 text-sm italic">
          {citation.quote}
        </blockquote>

        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block rounded bg-seal px-3 py-2 text-sm font-medium text-white hover:opacity-90"
          >
            Open official document
          </a>
        ) : (
          // AC-13: a suggestion citation carries no URL. Name the source, offer no broken link.
          <p className="text-sm text-ink-muted">
            Listed in the corpus as{" "}
            <span className="font-mono text-xs">{citation.doc_id}</span>.
          </p>
        )}
      </div>
    </>
  );
}
