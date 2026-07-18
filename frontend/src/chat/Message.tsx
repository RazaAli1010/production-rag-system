import type { Citation } from "../api/types";
import { Markdown } from "./Markdown";
import { RefusalCard } from "./RefusalCard";
import { StampTrail, WorkedChip } from "./StampTrail";
import { StreamErrorCard } from "./StreamErrorCard";
import type { Turn } from "./types";

/**
 * T8 — one exchange, set as a register entry: the query line, then the ruling beneath it.
 *
 * Deliberately NOT a chat bubble pair. This is a reference tool where the answer is the artifact
 * and the question is its heading, so the question is a full-width line in the condensed display
 * face and the answer gets the body width, the raised stock, and the seal rule down its edge.
 *
 * `dir="auto"` on every message body is what makes a mixed Urdu/Latin thread render correctly —
 * direction resolves per element from its first strong character, so a Roman-Urdu question and an
 * Urdu answer can sit in the same thread without either being forced (AC-37).
 */
export function Message({
  turn,
  onOpenCitation,
  onRetry,
}: {
  turn: Turn;
  onOpenCitation: (c: Citation, index: number) => void;
  onRetry: (turnId: string) => void;
}) {
  const streaming = turn.status === "streaming";
  const showTrail = streaming && !turn.trailCollapsed;

  return (
    <article className="mb-12">
      <header className="mb-4">
        <p className="mb-1 font-mono text-xs uppercase tracking-[0.14em] text-ink-muted">
          Asked
          {turn.namespace && (
            <span className="ml-2 text-seal">{turn.namespace.toUpperCase()} only</span>
          )}
        </p>
        <h2
          dir="auto"
          className="font-display text-lg font-bold leading-snug text-ink font-urdu-fallback"
        >
          {turn.question}
        </h2>
      </header>

      <div className="max-w-thread">
        {showTrail && <StampTrail stages={turn.stages} />}
        {!showTrail && turn.stages.length > 0 && (
          <WorkedChip stages={turn.stages} latencyMs={turn.meta?.latency_ms} />
        )}

        {turn.status === "refused" ? (
          <RefusalCard reason={turn.meta?.refusal_reason ?? null} suggestions={turn.citations} />
        ) : (
          <div className="rounded border-l-2 border-seal bg-paper-raised px-5 py-4">
            {/* The streaming answer. Polite, and separate from the trail's region so a stage
                change never re-announces the whole growing answer (AC-40). */}
            <div
              dir="auto"
              aria-live={streaming ? "polite" : "off"}
              aria-busy={streaming}
              className="font-urdu-fallback"
            >
              <Markdown
                text={turn.answer}
                citations={turn.citations}
                streaming={streaming}
                onOpenCitation={onOpenCitation}
              />
            </div>

            {turn.meta?.degraded && (
              <p className="mt-3 border-t border-rule pt-2 font-mono text-xs text-ink-muted">
                Searched the keyword index only — document search was unavailable.
              </p>
            )}
            {turn.meta?.memory_summarized && (
              <p className="mt-1 font-mono text-xs text-ink-muted">
                Ran on a condensed history of this chat.
              </p>
            )}
          </div>
        )}

        {(turn.status === "interrupted" || turn.status === "failed") && (
          <StreamErrorCard error={turn.error} onRetry={() => onRetry(turn.id)} />
        )}
      </div>
    </article>
  );
}
