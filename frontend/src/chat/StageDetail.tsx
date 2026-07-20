import type { StageDetail, TraceChunk } from "../api/types";

/**
 * The pipeline made visible: what each stage actually produced, rendered under its stamp.
 *
 * Every stage gets the shape that makes ITS work legible, not one generic table — reranking is
 * about movement so it shows two ordered columns and an arrow; compression is about loss so it
 * shows what was thrown away; hybrid is about two retrievers disagreeing so it shows both lists
 * side by side. A shared table would flatten exactly the differences worth showing.
 *
 * Everything here is read-only and derived from the `detail` payload on the stage's `done` frame,
 * so it renders identically live and when re-expanded from a finished turn's receipt.
 */

const pct = (n: number | null | undefined) => (n == null ? "—" : n.toFixed(3));

function Score({ value }: { value?: number | null }) {
  return <span className="shrink-0 font-mono text-[11px] text-ink-muted">{pct(value)}</span>;
}

/** One passage. `rank` is 1-based and shown because these lists are *ordered* — that's the point. */
function ChunkRow({ c, rank }: { c: TraceChunk; rank?: number }) {
  return (
    <li className="border-b border-rule/40 py-1 last:border-0">
      <div className="flex items-baseline gap-2">
        {rank != null && <span className="font-mono text-[11px] text-ink-muted">{rank}.</span>}
        <span className="min-w-0 flex-1 truncate text-[12px] text-ink">{c.title}</span>
        {c.moved != null && c.moved !== 0 && (
          <span
            className={`font-mono text-[11px] ${c.moved > 0 ? "text-seal" : "text-ink-muted"}`}
            title={c.moved > 0 ? `promoted ${c.moved} places` : `dropped ${-c.moved} places`}
          >
            {c.moved > 0 ? `▲${c.moved}` : `▼${-c.moved}`}
          </span>
        )}
        <Score value={c.score} />
      </div>
      <p className="mt-0.5 line-clamp-2 font-mono text-[11px] leading-snug text-ink-muted">
        {c.section ? `${c.section} · ` : ""}
        {c.page != null ? `p.${c.page} · ` : ""}
        {c.text}
      </p>
    </li>
  );
}

function Column({ title, chunks }: { title: string; chunks?: TraceChunk[] }) {
  return (
    <div className="min-w-0 flex-1">
      <p className="mb-1 font-mono text-[11px] uppercase tracking-[0.12em] text-ink-muted">
        {title} <span className="text-ink-muted/70">({chunks?.length ?? 0})</span>
      </p>
      <ul>
        {chunks?.map((c, i) => (
          <ChunkRow key={c.chunk_id} c={c} rank={i + 1} />
        ))}
      </ul>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <p className="text-[12px] text-ink">
      <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-ink-muted">
        {label}{" "}
      </span>
      {children}
    </p>
  );
}

function Searching({ d }: { d: StageDetail }) {
  return (
    <div className="flex flex-col gap-3">
      {d.runs?.map((run, i) => (
        <div key={i}>
          {/* Only worth naming the query when rewrite fanned out into several of them. */}
          {(d.runs?.length ?? 0) > 1 && (
            <p className="mb-1 font-mono text-[11px] text-seal">“{run.query}”</p>
          )}
          {run.degraded && (
            <p className="mb-1 font-mono text-[11px] text-flag">
              vector search failed — BM25 only
            </p>
          )}
          <div className="flex flex-wrap gap-4">
            <Column title="Vector" chunks={run.dense} />
            <Column title="BM25" chunks={run.sparse} />
            <Column title="Fused (RRF)" chunks={run.fused} />
          </div>
        </div>
      ))}
    </div>
  );
}

function Reranking({ d }: { d: StageDetail }) {
  return (
    <div className="flex flex-col gap-2">
      <Field label="Scored">
        {d.n_candidates} candidates → kept {d.kept}
      </Field>
      <div className="flex flex-wrap gap-4">
        <Column title="Before (fused)" chunks={d.before} />
        <Column title="After (cross-encoder)" chunks={d.after} />
      </div>
    </div>
  );
}

function Compressing({ d }: { d: StageDetail }) {
  const before = d.tokens_before ?? 0;
  const after = d.tokens_after ?? 0;
  const saved = before > 0 ? Math.round(((before - after) / before) * 100) : 0;
  return (
    <div className="flex flex-col gap-2">
      <Field label="Tokens">
        {before} → {after}{" "}
        <span className={saved > 0 ? "text-seal" : "text-ink-muted"}>({saved}% smaller)</span>
      </Field>
      <Field label="Chunks">
        {d.chunks_before} → {d.chunks_after} · {d.sentences_dropped ?? 0} sentences dropped
      </Field>
      {d.dropped && d.dropped.length > 0 && (
        <div>
          <p className="mb-1 font-mono text-[11px] uppercase tracking-[0.12em] text-ink-muted">
            Discarded ({d.dropped.length})
          </p>
          <ul className="opacity-50">
            {d.dropped.map((c) => (
              <ChunkRow key={c.chunk_id} c={c} />
            ))}
          </ul>
        </div>
      )}
      {d.trimmed && d.trimmed.length > 0 && (
        <div>
          <p className="mb-1 font-mono text-[11px] uppercase tracking-[0.12em] text-ink-muted">
            Trimmed ({d.trimmed.length})
          </p>
          <ul>
            {d.trimmed.map((t) => (
              <li key={t.chunk_id} className="border-b border-rule/40 py-1 last:border-0">
                <div className="flex items-baseline gap-2">
                  <span className="min-w-0 flex-1 truncate text-[12px] text-ink">{t.title}</span>
                  <span className="shrink-0 font-mono text-[11px] text-ink-muted">
                    {t.tokens_before}→{t.tokens_after}
                  </span>
                </div>
                <p className="mt-0.5 line-clamp-2 font-mono text-[11px] leading-snug text-ink-muted">
                  {t.text_after}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Rewriting({ d }: { d: StageDetail }) {
  return (
    <div className="flex flex-col gap-1">
      <Field label="Asked">{d.original}</Field>
      <Field label="Searched">
        <span className="text-seal">{d.normalized}</span>
      </Field>
      {d.variants && d.variants.length > 0 && (
        <Field label="Also">
          <span className="font-mono text-[11px]">{d.variants.join(" · ")}</span>
        </Field>
      )}
      {d.language && <Field label="Language">{d.language}</Field>}
      {d.failed && <p className="font-mono text-[11px] text-flag">rewrite failed — used raw query</p>}
    </div>
  );
}

function CacheLookup({ d }: { d: StageDetail }) {
  return (
    <div className="flex flex-col gap-1">
      <Field label="Result">
        <span className={d.hit ? "text-seal" : "text-ink-muted"}>
          {d.hit ? `hit (${d.tier})` : "miss"}
        </span>
      </Field>
      <Field label="Key">
        <span className="font-mono text-[11px]">{d.key}</span>
      </Field>
      <Field label="Cached">{d.n_entries} entries</Field>
    </div>
  );
}

function Generating({ d }: { d: StageDetail }) {
  return (
    <div className="flex flex-col gap-2">
      <Field label="Model">
        {d.model} · {d.tokens_out} tokens out{d.memory_used ? " · with history" : ""}
      </Field>
      <Column title="Context used" chunks={d.context} />
    </div>
  );
}

function Summarizing({ d }: { d: StageDetail }) {
  return (
    <div className="flex flex-col gap-1">
      <Field label="Folded">{d.pairs_folded} earlier exchanges</Field>
      <p className="text-[12px] leading-snug text-ink-muted">{d.summary}</p>
    </div>
  );
}

const VIEWS: Record<string, (p: { d: StageDetail }) => React.ReactElement> = {
  searching: Searching,
  reranking: Reranking,
  compressing: Compressing,
  rewriting: Rewriting,
  cache_lookup: CacheLookup,
  generating: Generating,
  summarizing_memory: Summarizing,
};

/** Renders nothing for a stage with no view and no payload, so an unknown stage stays harmless. */
export function StageDetailView({ stage, detail }: { stage: string; detail?: StageDetail | null }) {
  const View = VIEWS[stage];
  if (!View || !detail) return null;
  return (
    <div className="mt-1 border-l-2 border-stamp/30 pl-3">
      <View d={detail} />
    </div>
  );
}

export const hasDetail = (stage: string, detail?: StageDetail | null) =>
  Boolean(detail) && stage in VIEWS;
