/**
 * Stage labels. Present participle, plain, in the interface's voice — these are read while waiting,
 * so they say what is happening rather than naming the module doing it.
 *
 * `stage` is a bare `str` on the wire, not a Literal, so an unlabelled id must survive: the fallback
 * humanises the raw token rather than dropping the event (AC-4).
 */
const LABELS: Record<string, string> = {
  rewriting: "Rewriting your question",
  cache_lookup: "Checking recent answers",
  searching: "Searching documents",
  reranking: "Reranking results",
  compressing: "Trimming the context",
  generating: "Writing the answer",
  citing: "Attaching citations",
  summarizing_memory: "Condensing earlier conversation",
};

export function stageLabel(stage: string): string {
  return LABELS[stage] ?? stage.replace(/_/g, " ");
}

/** `2140` → `2.1s`; `840` → `0.8s`. */
export function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}
