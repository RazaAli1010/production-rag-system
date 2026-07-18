import { useEffect, useRef, useState } from "react";
import type { Citation } from "../api/types";
import { Message } from "./Message";
import type { Turn } from "./types";

/** Six openers from the real corpus — three English, three code-switched (requirements §6). */
export const EXAMPLES = [
  "What CGPA puts me on probation?",
  "probation se kaise nikalta hoon?",
  "How do I get my degree attested by HEC?",
  "fee refund ka rule kya hai agar semester drop karun?",
  "What counts as plagiarism under the HEC policy?",
  "attendance 75% se kam ho to exam de sakta hoon?",
];

/** The split is real information, not decoration: both registers work, and students hesitate to
 *  type the second one until they see it offered. */
const EXAMPLES_BY_REGISTER = [
  { label: "In English", questions: EXAMPLES.filter((_, i) => i % 2 === 0) },
  { label: "Roman Urdu", questions: EXAMPLES.filter((_, i) => i % 2 === 1) },
];

interface Props {
  turns: Turn[];
  onOpenCitation: (c: Citation, index: number) => void;
  onRetry: (turnId: string) => void;
  onPickExample: (q: string) => void;
}

/** T8 — the scrollable thread, with pinned-to-bottom autoscroll (AC-6). */
export function Thread({ turns, onOpenCitation, onRetry, onPickExample }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);

  // Autoscroll ONLY while the user is already at the bottom. Yanking someone back down while they
  // are reading an earlier answer is the single most irritating thing a streaming UI can do.
  useEffect(() => {
    if (!pinned) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, pinned]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  };

  return (
    <div className="relative flex-1 overflow-hidden">
      <div ref={scrollRef} onScroll={onScroll} className="h-full overflow-y-auto px-4 py-6">
        <div className="mx-auto max-w-thread">
          {turns.length === 0 ? (
            <div className="pt-6">
              <h1 className="font-display text-xl font-bold leading-tight">
                Ask about PU and HEC rules.
                <br />
                Every answer cites its page.
              </h1>

              {/* Naming the two corpora is the credibility claim — it says what this can and
                  cannot answer, which is the honest version of a feature list. */}
              <dl className="mt-5 grid gap-x-6 gap-y-2 border-y border-rule py-4 text-sm sm:grid-cols-2">
                <div>
                  <dt className="font-mono text-xs uppercase tracking-[0.14em] text-seal">
                    University of the Punjab
                  </dt>
                  <dd className="text-ink-muted">Calendar, statutes, and examination rules.</dd>
                </div>
                <div>
                  <dt className="font-mono text-xs uppercase tracking-[0.14em] text-seal">HEC</dt>
                  <dd className="text-ink-muted">
                    Attestation, plagiarism, and degree-equivalence policy.
                  </dd>
                </div>
              </dl>

              <p className="mt-6 text-sm text-ink-muted">
                Type in English, Urdu, or both. Start with one of these:
              </p>
              <div className="mt-3 grid gap-x-6 gap-y-5 sm:grid-cols-2">
                {EXAMPLES_BY_REGISTER.map(({ label, questions }) => (
                  <section key={label}>
                    <h2 className="mb-2 font-mono text-xs uppercase tracking-[0.14em] text-ink-muted">
                      {label}
                    </h2>
                    <ul className="flex flex-col gap-1.5">
                      {questions.map((q) => (
                        <li key={q}>
                          <button
                            type="button"
                            dir="auto"
                            onClick={() => onPickExample(q)}
                            className="font-urdu-fallback w-full border-l-2 border-rule py-1 pl-3
                                       text-left text-sm text-ink transition-colors
                                       hover:border-seal hover:text-seal"
                          >
                            {q}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </section>
                ))}
              </div>
            </div>
          ) : (
            turns.map((t) => (
              <Message key={t.id} turn={t} onOpenCitation={onOpenCitation} onRetry={onRetry} />
            ))
          )}
        </div>
      </div>

      {!pinned && turns.length > 0 && (
        <button
          type="button"
          onClick={() => setPinned(true)}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full border border-rule
                     bg-paper-raised px-3 py-1.5 text-xs font-medium shadow-lg"
        >
          Jump to latest
        </button>
      )}
    </div>
  );
}
