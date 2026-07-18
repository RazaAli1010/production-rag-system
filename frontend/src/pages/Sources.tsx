import { useEffect, useState } from "react";
import { fetchJson } from "../api/client";
import type { DocumentOut } from "../api/types";
import { Header } from "../ui/Header";

/** T19 — the corpus, as a trust builder: this is exactly what the assistant can read. */
export function Sources() {
  const [docs, setDocs] = useState<DocumentOut[] | null>(null);

  useEffect(() => {
    void fetchJson<DocumentOut[]>("/api/documents")
      .then(setDocs)
      .catch(() => setDocs([]));
  }, []);

  const groups = ["PU", "HEC"] as const;

  return (
    <div className="flex h-dvh flex-col overflow-hidden">
      <Header />
      <main className="flex-1 overflow-y-auto px-4 py-8">
        <div className="mx-auto max-w-thread">
          <h1 className="font-display text-xl font-bold">What the assistant reads</h1>
          <p className="mt-2 text-sm text-ink-muted">
            Every answer is drawn from these documents and cites the page it came from. Nothing else
            is consulted.
          </p>

          {docs === null ? (
            <p className="mt-8 text-sm text-ink-muted">Loading the corpus…</p>
          ) : docs.length === 0 ? (
            <p className="mt-8 text-sm text-ink-muted">
              No documents are indexed yet. Answers will refuse until the corpus is loaded.
            </p>
          ) : (
            groups.map((org) => {
              const rows = docs.filter((d) => d.source_org === org);
              if (!rows.length) return null;
              return (
                <section key={org} className="mt-8">
                  <h2 className="font-display text-sm font-bold uppercase tracking-wider text-ink-muted">
                    {org === "PU" ? "University of the Punjab" : "Higher Education Commission"}
                  </h2>
                  <ul className="mt-3 divide-y divide-rule border-y border-rule">
                    {rows.map((d) => (
                      <li key={d.doc_id} className="flex items-baseline gap-3 py-3">
                        <div className="min-w-0 flex-1">
                          <a
                            href={d.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-sm font-medium underline underline-offset-2"
                          >
                            {d.title}
                          </a>
                          <p className="font-mono text-xs text-ink-muted">{d.doc_id}</p>
                        </div>
                        <span className="shrink-0 font-mono text-xs text-ink-muted">
                          {d.version_label} · {d.file_type}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })
          )}
        </div>
      </main>
    </div>
  );
}
