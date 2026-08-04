import type { Citation } from "../api/types";
import { Markdown } from "./Markdown";
import { RefusalCard } from "./RefusalCard";
import { stageLabel } from "./stages";
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
/**
 * The pending mark, shown in the answer card between the ask and the first token — a wait that runs
 * tens of seconds, so the card can't sit blank. It's the seal die itself, turning one detent at a
 * time: the trail above says WHAT is happening, this only says work is still live.
 *
 * Inked in all three accents at once — eight spokes over a three-colour cycle never repeats around
 * the ring, so every detent lands a different colour under the same spoke and the die reads as a
 * multi-pad stamp rather than a tinted spinner.
 */
const SPOKE_INK = ["fill-stamp", "fill-seal", "fill-flag"];

function PressingSeal({ label }: { label: string }) {
  // Turn and ink are split across the two elements: one `animation` shorthand each, so the two
  // utilities never fight over the same property.
  return (
    // Visual only: the trail's sr-only live region already announces every stage, and this sits
    // inside the answer's own polite region — announcing here would read each stage twice.
    <span aria-hidden="true" className="flex animate-ink-breathe items-center gap-3 py-1">
      <svg viewBox="0 0 24 24" aria-hidden="true" className="h-10 w-10 shrink-0 animate-seal-spin">
        {[0, 45, 90, 135, 180, 225, 270, 315].map((deg, i) => (
          // Tapered wedge: narrow at the rim, wide at the hub, stopping short of centre so the die
          // reads as a hollow ring of spokes rather than a filled asterisk.
          <path
            key={deg}
            d="M12 1.8 L13.15 9.4 L10.85 9.4 Z"
            transform={`rotate(${deg} 12 12)`}
            className={SPOKE_INK[i % SPOKE_INK.length]}
          />
        ))}
        {/* The registration mark. Eight spokes at eight detents map onto themselves — without an
            asymmetric mark the die would step 45° and look perfectly frozen. */}
        <circle cx="15.4" cy="3.7" r="1.4" className="fill-flag" />
      </svg>
      {/* The word beside the die is the live stage, not a random verb — it changes on its own
          through the wait because the pipeline actually moves through those stages. */}
      <span className="font-display text-base font-medium text-ink-muted">{label}…</span>
    </span>
  );
}

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
          // No card at all when there's nothing to put in it and nothing coming — otherwise an
          // interrupted or failed turn leaves an empty ruled box above its error card.
          (turn.answer || streaming) && (
            <div className="rounded border-l-2 border-seal bg-paper-raised px-5 py-4">
              {/* The streaming answer. Polite, and separate from the trail's region so a stage
                  change never re-announces the whole growing answer (AC-40). */}
              <div
                dir="auto"
                aria-live={streaming ? "polite" : "off"}
                aria-busy={streaming}
                className="font-urdu-fallback"
              >
                {turn.answer ? (
                  <Markdown
                    text={turn.answer}
                    citations={turn.citations}
                    streaming={streaming}
                    onOpenCitation={onOpenCitation}
                  />
                ) : (
                  <PressingSeal
                    label={
                      turn.stages.length
                        ? stageLabel(turn.stages[turn.stages.length - 1]!.stage)
                        : "Working on it"
                    }
                  />
                )}
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
          )
        )}

        {(turn.status === "interrupted" || turn.status === "failed") && (
          <StreamErrorCard error={turn.error} onRetry={() => onRetry(turn.id)} />
        )}
      </div>
    </article>
  );
}
