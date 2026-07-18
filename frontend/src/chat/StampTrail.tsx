import { useState } from "react";
import { totalStageMs, type TrailStage } from "./types";
import { seconds, stageLabel } from "./stages";

/**
 * T9 — the signature element (AC-3, AC-4, AC-5, AC-40, AC-43).
 *
 * Each stage is a stamp impression: violet ink on paper, alternating a fraction of a degree so the
 * trail reads as pressed rather than typeset. This is the one place the design spends its boldness;
 * everything around it stays flat and quiet.
 *
 * The trail is ordered and connected because the pipeline genuinely IS a sequence — order carries
 * meaning here, which is exactly the test for whether a structural device belongs.
 */

function Stamp({ stage, live }: { stage: TrailStage; live: boolean }) {
  const skipped = stage.status === "skipped";
  const running = stage.status === "started";
  return (
    <li
      className={[
        "flex items-baseline gap-2 rounded-[3px] border px-2 py-1",
        live ? "animate-press" : "",
        skipped
          ? "border-rule/60 bg-transparent text-ink-muted opacity-40"
          : "border-stamp/40 bg-stamp/[0.07] text-ink",
      ].join(" ")}
      style={{ transform: `rotate(${stage.stage.length % 2 === 0 ? "-0.6deg" : "0.6deg"})` }}
    >
      <span className={`text-sm ${skipped ? "line-through" : ""}`}>{stageLabel(stage.stage)}</span>
      {running && (
        <span className="font-mono text-xs text-ink-muted" aria-hidden="true">
          …
        </span>
      )}
      {stage.ms != null && !skipped && (
        <span className="ml-auto font-mono text-xs text-ink-muted">{stage.ms}ms</span>
      )}
    </li>
  );
}

/** The live trail, shown while the answer is still being worked out. */
export function StampTrail({ stages }: { stages: TrailStage[] }) {
  return (
    <div className="my-2">
      {/* Announces label transitions only. `ms` values are deliberately outside the live region —
          a screen-reader user does not need "380 milliseconds" read aloud eight times. */}
      <p className="sr-only" aria-live="polite">
        {stages.length ? stageLabel(stages[stages.length - 1]!.stage) : ""}
      </p>
      <ul className="flex max-w-sm flex-col gap-1.5">
        {stages.map((s, i) => (
          <Stamp key={`${s.stage}-${i}`} stage={s} live={i === stages.length - 1} />
        ))}
      </ul>
    </div>
  );
}

/**
 * The receipt. Once the answer starts arriving the trail folds into one chip that stays on the
 * finished message permanently — proof of what the system did, expandable to the timings.
 */
export function WorkedChip({ stages, latencyMs }: { stages: TrailStage[]; latencyMs?: number }) {
  const [open, setOpen] = useState(false);
  const total = latencyMs ?? totalStageMs(stages);
  if (!stages.length) return null;

  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex rotate-[-0.6deg] items-center gap-1.5 rounded-[3px] border
                   border-stamp/40 bg-stamp/[0.07] px-2 py-0.5 font-mono text-xs text-ink-muted
                   hover:bg-stamp/[0.14]"
      >
        worked {seconds(total)}
        <span aria-hidden="true">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <ul className="mt-2 flex max-w-sm flex-col gap-1.5">
          {stages.map((s, i) => (
            <Stamp key={`${s.stage}-${i}`} stage={s} live={false} />
          ))}
        </ul>
      )}
    </div>
  );
}
