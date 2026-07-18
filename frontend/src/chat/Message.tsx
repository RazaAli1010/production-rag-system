import type { Citation } from "../api/types";
import { Markdown } from "./Markdown";
import { RefusalCard } from "./RefusalCard";
import { StampTrail, WorkedChip } from "./StampTrail";
import { StreamErrorCard } from "./StreamErrorCard";
import type { Turn } from "./types";

/**
 * T8 — one exchange: the question right, the answer left.
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
    <article className="mb-8">
      <div className="mb-3 flex justify-end">
        <p
          dir="auto"
          className="max-w-[85%] rounded bg-ink px-3 py-2 text-sm text-paper font-urdu-fallback"
        >
          {turn.question}
        </p>
      </div>

      <div className="max-w-thread">
        {showTrail && <StampTrail stages={turn.stages} />}
        {!showTrail && turn.stages.length > 0 && (
          <WorkedChip stages={turn.stages} latencyMs={turn.meta?.latency_ms} />
        )}

        {turn.status === "refused" ? (
          <RefusalCard reason={turn.meta?.refusal_reason ?? null} suggestions={turn.citations} />
        ) : (
          <div className="rounded bg-paper-raised px-4 py-3">
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
