import { useEffect, useState } from "react";
import { fetchJson } from "../api/client";
import type { ApiError } from "../api/errors";
import type { StatsResponse } from "../api/types";
import { Header } from "../ui/Header";

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded border border-rule bg-paper-raised p-4">
      <p className="text-xs uppercase tracking-wide text-ink-muted">{label}</p>
      {/* Mono so figures line up column to column and can actually be compared. */}
      <p className="mt-1 font-mono text-lg">{value}</p>
      {sub && <p className="mt-0.5 font-mono text-xs text-ink-muted">{sub}</p>}
    </div>
  );
}

/** T20 — admin stats (AC-35). The route guard is presentation; `/internal/*` is admin-gated server-side. */
export function Admin() {
  const [window, setWindow] = useState<"24h" | "7d">("24h");
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    setStats(null);
    setError(null);
    void fetchJson<StatsResponse>(`/internal/stats?window=${window}`)
      .then(setStats)
      .catch((e) => setError(e as ApiError));
  }, [window]);

  return (
    <div className="flex h-dvh flex-col overflow-hidden">
      <Header />
      <main className="flex-1 overflow-y-auto px-4 py-8">
        <div className="mx-auto max-w-4xl">
          <div className="flex items-baseline justify-between gap-4">
            <h1 className="font-display text-xl font-bold">Service stats</h1>
            <div className="flex gap-1.5">
              {(["24h", "7d"] as const).map((w) => (
                <button
                  key={w}
                  type="button"
                  onClick={() => setWindow(w)}
                  aria-pressed={window === w}
                  className={`rounded-full border px-3 py-1 font-mono text-xs ${
                    window === w ? "border-seal bg-seal text-white" : "border-rule text-ink-muted"
                  }`}
                >
                  {w}
                </button>
              ))}
            </div>
          </div>

          {error ? (
            <p role="alert" className="mt-6 rounded border border-flag/40 bg-flag/[0.07] p-3 text-sm">
              {error.message}
            </p>
          ) : !stats ? (
            <p className="mt-6 text-sm text-ink-muted">Loading stats…</p>
          ) : (
            <>
              <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                <Stat label="Requests" value={stats.request_count.toLocaleString()} />
                <Stat
                  label="Latency"
                  value={stats.p50_ms == null ? "—" : `${stats.p50_ms}ms`}
                  sub={stats.p95_ms == null ? undefined : `p95 ${stats.p95_ms}ms`}
                />
                <Stat label="Cache hits" value={pct(stats.cache_hit_rate)} />
                <Stat label="Refusals" value={pct(stats.refusal_rate)} />
                <Stat label="Errors" value={pct(stats.error_rate)} />
                <Stat label="Degraded" value={pct(stats.degraded_rate)} />
                <Stat label="Spend" value={`$${stats.total_cost_usd.toFixed(2)}`} />
                <Stat
                  label="Tokens saved"
                  value={stats.tokens_saved_by_cache.toLocaleString()}
                  sub="by cache"
                />
                <Stat label="Active sessions" value={String(stats.active_sessions)} />
                <Stat label="Turns / session" value={stats.mean_turns_per_session.toFixed(1)} />
                <Stat label="Summarisations" value={String(stats.summarization_count)} />
                <Stat
                  label="Tokens saved"
                  value={stats.tokens_saved_by_summarization_est.toLocaleString()}
                  sub="by summarising"
                />
              </div>

              <section className="mt-8">
                <h2 className="font-display text-sm font-bold uppercase tracking-wider text-ink-muted">
                  Enhancements used
                </h2>
                <ul className="mt-2 flex flex-wrap gap-2">
                  {Object.entries(stats.flag_usage).map(([flag, n]) => (
                    <li
                      key={flag}
                      className="rounded border border-rule bg-paper-raised px-2 py-1 font-mono text-xs"
                    >
                      {flag} <span className="text-ink-muted">{n.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
              </section>

              {stats.top_query_clusters.length > 0 && (
                <section className="mt-8">
                  <h2 className="font-display text-sm font-bold uppercase tracking-wider text-ink-muted">
                    What students ask
                  </h2>
                  <ul className="mt-2 divide-y divide-rule border-y border-rule">
                    {stats.top_query_clusters.map((c, i) => (
                      <li key={i} className="flex justify-between py-2 text-sm">
                        <span>{String((c as Record<string, unknown>).cluster ?? "—")}</span>
                        <span className="font-mono text-ink-muted">
                          {String((c as Record<string, unknown>).count ?? "")}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
