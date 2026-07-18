import type { ApiError } from "../api/errors";

/**
 * T13 — something broke (AC-26, AC-27).
 *
 * Errors say what happened and what to do. They never apologise and are never vague. When partial
 * text survived, this sits BELOW it — the answer so far is not discarded.
 */
export function StreamErrorCard({ error, onRetry }: { error?: ApiError; onRetry: () => void }) {
  const message =
    error?.type === "disconnected"
      ? "The answer stopped partway. The connection dropped."
      : (error?.message ?? "The answer stopped partway.");

  return (
    <div className="mt-3 flex flex-wrap items-center gap-3 rounded border border-rule bg-paper p-3">
      <p className="text-sm text-ink-muted">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="rounded border border-seal px-3 py-1 text-sm font-medium text-seal hover:bg-seal hover:text-white"
      >
        Try again
      </button>
    </div>
  );
}
