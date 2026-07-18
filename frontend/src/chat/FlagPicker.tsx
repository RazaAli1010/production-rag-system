import type { Flags } from "./useFlags";

/** Order is the pipeline order (rewrite → cache → retrieve → rerank → compress → generate), so the
 *  row reads as the path a question takes. `deep` is last because it changes the model, not a stage. */
const CHIPS: { id: keyof Flags; label: string; hint: string }[] = [
  { id: "hybrid", label: "Hybrid search", hint: "Match meaning and exact wording, not just meaning." },
  { id: "rerank", label: "Rerank", hint: "Re-score the passages found so the best ones lead." },
  { id: "query_rewrite", label: "Query rewrite", hint: "Rephrase your question into a few searches first. Slower." },
  { id: "compression", label: "Compression", hint: "Trim the passages down to the sentences that matter." },
  { id: "cache", label: "Cache", hint: "Reuse an earlier answer to a near-identical question." },
  { id: "memory", label: "Memory", hint: "Remember this conversation so follow-ups make sense." },
  { id: "deep", label: "Deep mode", hint: "Use the stronger model. Slower and costs more." },
];

interface Props {
  flags: Flags;
  onToggle: (key: keyof Flags, value: boolean) => void;
  /** Locked once the conversation has a turn: mixing pipelines mid-thread makes the answers
   *  incomparable, and memory in particular can't be switched on retroactively. */
  locked?: boolean;
}

export function FlagPicker({ flags, onToggle, locked }: Props) {
  return (
    <div
      className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 font-mono text-xs"
      role="group"
      aria-label="Choose how this conversation is answered"
    >
      <span className="uppercase tracking-[0.14em] text-ink-muted">Pipeline</span>
      {CHIPS.map((c) => (
        <button
          key={c.id}
          type="button"
          disabled={locked}
          onClick={() => onToggle(c.id, !flags[c.id])}
          aria-pressed={flags[c.id]}
          title={locked ? "Locked for this conversation — start a new chat to change" : c.hint}
          className={`border-b transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
            flags[c.id]
              ? "border-seal font-medium text-seal"
              : "border-transparent text-ink-muted hover:text-ink"
          }`}
        >
          {c.label}
        </button>
      ))}
      {locked && <span className="text-ink-muted">· locked for this chat</span>}
    </div>
  );
}
