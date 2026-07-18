import type { Citation } from "../api/types";
import { sourceHref } from "./CitationPanel";

/**
 * `refusal_reason` is a MACHINE TOKEN on the wire, not prose — the backend emits
 * "low_retrieval_confidence" (baseline.py:260) and "no_grounded_claims" (baseline.py:334,
 * refusal.py:57). Rendering it raw would put an identifier in front of a student, which is the same
 * mistake as showing them "session_busy". Translate, and fall back for a token we don't know.
 */
const REASONS: Record<string, string> = {
  low_retrieval_confidence:
    "Nothing in the PU or HEC documents matched this closely enough to answer from.",
  no_grounded_claims:
    "The documents that came back didn't actually cover this, so there was nothing to cite.",
};

export function refusalCopy(reason: string | null): string {
  if (!reason) return "There was nothing in these documents to answer from.";
  return REASONS[reason] ?? "There was nothing in these documents to answer from.";
}

/**
 * T13 — a refusal is a VALID answer (AC-24).
 *
 * Visually distinct from both a normal answer and an error: bordered in `--flag` but on raised
 * paper, no alarm iconography, no apology. It states what was searched and offers where to look.
 * Styling this as a failure would teach students to distrust a system behaving exactly as designed.
 */
export function RefusalCard({
  reason,
  suggestions,
}: {
  reason: string | null;
  suggestions: Citation[];
}) {
  return (
    <div className="rounded border border-flag/40 bg-flag/[0.06] p-4">
      <p className="font-display text-sm font-bold uppercase tracking-wide text-flag">
        Not in these documents
      </p>
      <p className="mt-2 text-sm">{refusalCopy(reason)}</p>
      {suggestions.length > 0 && (
        <>
          <p className="mt-4 text-sm font-semibold">You might check</p>
          <ul className="mt-1 space-y-1">
            {suggestions.map((c) => {
              const href = sourceHref(c);
              return (
                <li key={c.chunk_id} className="text-sm">
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-seal underline underline-offset-2"
                    >
                      {c.title}
                    </a>
                  ) : (
                    <span>{c.title}</span>
                  )}
                  {c.section_heading && (
                    <span className="text-ink-muted"> — {c.section_heading}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}
