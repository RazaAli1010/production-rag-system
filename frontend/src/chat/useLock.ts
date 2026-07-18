import { useEffect, useState } from "react";

/**
 * T15 — the composer lock (AC-25, AC-28).
 *
 * One mechanism, two reasons. A 429 counts down from `Retry-After` because the user needs to know
 * how long; a 409 is a short automatic lock because the in-flight turn may belong to another tab
 * and this client cannot observe when it ends. Neither is styled as an error.
 */
export function useLock(busyUntil: number | null, reason: "rate_limited" | "session_busy" | null) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!busyUntil || busyUntil <= Date.now()) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [busyUntil]);

  const remainingMs = busyUntil ? busyUntil - now : 0;
  const locked = remainingMs > 0;
  const seconds = Math.ceil(remainingMs / 1000);

  const note = !locked
    ? null
    : reason === "session_busy"
      ? "Finishing your last question…"
      : `Too many questions. Try again in ${seconds}s.`;

  return { locked, seconds, note };
}
