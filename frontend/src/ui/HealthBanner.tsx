import { useEffect, useState } from "react";
import type { HealthResponse } from "../api/types";

/**
 * T22 — dependency degradation notice (AC-29).
 *
 * `GET /api/health` returns HTTP **503** when a core dependency is down, with the detail in the
 * body — so this reads the body of a FAILED response, which a naive `res.ok` check would discard.
 * It is informational and dismissible, never blocking: a degraded Pinecone still answers from BM25.
 */
export function HealthBanner() {
  const [down, setDown] = useState<string[]>([]);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch("/api/health", { credentials: "include" });
        const body = (await res.json()) as HealthResponse;
        if (!alive || body.status === "ok") return;
        setDown(
          Object.entries(body.dependencies)
            .filter(([, v]) => v !== "ok" && v !== "skipped")
            .map(([k]) => k),
        );
      } catch {
        /* the health probe failing is not itself worth a banner */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (dismissed || down.length === 0) return null;

  return (
    <div className="flex items-center gap-3 border-b border-flag/30 bg-flag/[0.07] px-4 py-2 text-sm">
      <p className="flex-1">
        Some search services are down ({down.join(", ")}). Answers may be less complete than usual.
      </p>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        className="rounded px-2 py-0.5 text-xs text-ink-muted hover:text-ink"
      >
        Dismiss
      </button>
    </div>
  );
}
