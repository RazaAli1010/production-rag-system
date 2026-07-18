import { useEffect, useRef, useState } from "react";

export type Namespace = "all" | "pu" | "hec";

const MIN = 3;
const MAX = 500; // both bounds are server-enforced (AskRequest), so the UI mirrors them exactly

const CHIPS: { id: Namespace; label: string }[] = [
  { id: "all", label: "All" },
  { id: "pu", label: "PU" },
  { id: "hec", label: "HEC" },
];

interface Props {
  onAsk: (question: string, namespace?: "pu" | "hec") => void;
  disabled?: boolean;
  /** Shown in place of the counter while the composer is locked. */
  lockNote?: string | null;
  /** Seeded when an example question is picked; the caller remounts to re-seed. */
  initialValue?: string;
}

/** T12 — the composer (AC-2, AC-11, AC-41). */
export function Composer({ onAsk, disabled, lockNote, initialValue = "" }: Props) {
  const [value, setValue] = useState(initialValue);
  const [ns, setNs] = useState<Namespace>("all");
  const ref = useRef<HTMLTextAreaElement>(null);

  const trimmed = value.trim();
  const tooShort = trimmed.length > 0 && trimmed.length < MIN;
  const tooLong = value.length > MAX;
  const canSend = !disabled && trimmed.length >= MIN && !tooLong;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [value]);

  const submit = () => {
    if (!canSend) return;
    onAsk(trimmed, ns === "all" ? undefined : ns); // AC-11: "All" omits `namespace` entirely
    setValue("");
  };

  return (
    <div className="border-t border-rule bg-paper px-4 pb-3 pt-2">
      <div className="mx-auto max-w-thread">
        {/* Scope is a refinement, not the main event: it sits as a quiet mono row above the field
            so the send affordance keeps the weight. */}
        <div
          className="mb-2 flex items-baseline gap-3 font-mono text-xs"
          role="group"
          aria-label="Limit to a source"
        >
          <span className="uppercase tracking-[0.14em] text-ink-muted">Search</span>
          {CHIPS.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => setNs(c.id)}
              aria-pressed={ns === c.id}
              className={`border-b transition-colors ${
                ns === c.id
                  ? "border-seal font-medium text-seal"
                  : "border-transparent text-ink-muted hover:text-ink"
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>

        <div className="flex items-end gap-2">
          <textarea
            ref={ref}
            dir="auto"
            rows={1}
            value={value}
            disabled={disabled}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends, Shift+Enter breaks the line (AC-41).
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Ask about PU or HEC rules…"
            aria-label="Your question"
            aria-describedby="composer-counter"
            className="font-urdu-fallback max-h-40 flex-1 resize-none rounded border border-rule
                       bg-paper-raised px-3 py-2 text-base disabled:opacity-60"
          />
          <button
            type="button"
            onClick={submit}
            disabled={!canSend}
            className="shrink-0 rounded bg-seal px-6 py-2.5 text-sm font-semibold text-white
                       transition-opacity hover:opacity-90
                       disabled:cursor-not-allowed disabled:opacity-40"
          >
            Ask
          </button>
        </div>

        <div className="mt-1.5 flex items-baseline justify-between gap-3">
          <p id="composer-counter" className="font-mono text-xs text-ink-muted">
            {lockNote ? (
              <span className="text-flag">{lockNote}</span>
            ) : tooShort ? (
              <span className="text-flag">At least {MIN} characters</span>
            ) : (
              <span className={tooLong ? "text-flag" : ""}>
                {value.length}/{MAX}
              </span>
            )}
          </p>
          <p className="text-right text-xs text-ink-muted">Check the cited page before you act.</p>
        </div>
      </div>
    </div>
  );
}
