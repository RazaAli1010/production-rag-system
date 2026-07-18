import { useState } from "react";
import { Link } from "react-router-dom";
import type { SessionOut } from "../api/types";

/** "18 minutes ago" beats a timestamp for something you left an hour ago. */
export function relativeTime(iso: string, now = Date.now()): string {
  const diff = Math.max(0, now - Date.parse(iso));
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

interface Props {
  sessions: SessionOut[];
  activeId: string | null;
  authed: boolean;
  open: boolean;
  onClose(): void;
  onNewChat(): void;
  onOpenSession(id: string): void;
  onDelete(id: string): void;
}

/**
 * T16 — session list (AC-19, AC-20, AC-22, AC-23).
 *
 * There is no rename: the backend exposes no `PATCH /api/sessions/{id}`, so the server-assigned
 * title is read-only here (requirements §9-2). Offering a rename control that cannot persist would
 * be worse than not offering one.
 */
export function Sidebar({
  sessions,
  activeId,
  authed,
  open,
  onClose,
  onNewChat,
  onOpenSession,
  onDelete,
}: Props) {
  const [confirming, setConfirming] = useState<string | null>(null);

  return (
    <>
      {open && (
        <div className="fixed inset-0 z-30 bg-ink/30 lg:hidden" onClick={onClose} aria-hidden="true" />
      )}
      <aside
        aria-label="Your chats"
        className={`fixed inset-y-0 left-0 z-40 flex w-72 flex-col border-r border-rule bg-paper-raised
                    transition-transform lg:static lg:translate-x-0
                    ${open ? "translate-x-0" : "-translate-x-full"}`}
      >
        <div className="border-b border-rule p-3">
          <button
            type="button"
            onClick={() => {
              onNewChat();
              onClose();
            }}
            className="w-full rounded border border-seal px-3 py-2 text-sm font-medium text-seal hover:bg-seal hover:text-white"
          >
            New chat
          </button>
        </div>

        <nav aria-label="Chat history" className="flex-1 overflow-y-auto p-2">
          {!authed ? (
            <div className="p-3">
              <p className="text-sm text-ink-muted">
                This chat disappears when you close the tab.
              </p>
              <Link
                to="/login"
                className="mt-2 inline-block text-sm font-medium text-seal underline underline-offset-2"
              >
                Log in to keep your chats
              </Link>
            </div>
          ) : sessions.length === 0 ? (
            <p className="p-3 text-sm text-ink-muted">No saved chats yet.</p>
          ) : (
            <ul className="space-y-0.5">
              {sessions.map((s) => (
                <li key={s.id}>
                  <div
                    className={`group flex items-center gap-1 rounded px-2 py-1.5 ${
                      s.id === activeId ? "bg-paper" : "hover:bg-paper"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => {
                        onOpenSession(s.id);
                        onClose();
                      }}
                      className="min-w-0 flex-1 text-left"
                    >
                      <span className="block truncate text-sm">{s.title ?? "Untitled chat"}</span>
                      <span className="block font-mono text-xs text-ink-muted">
                        {relativeTime(s.last_active_at)}
                      </span>
                    </button>
                    {confirming === s.id ? (
                      <span className="flex shrink-0 gap-1">
                        <button
                          type="button"
                          onClick={() => {
                            onDelete(s.id);
                            setConfirming(null);
                          }}
                          className="rounded px-1.5 py-0.5 text-xs font-medium text-flag"
                        >
                          Delete
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirming(null)}
                          className="rounded px-1.5 py-0.5 text-xs text-ink-muted"
                        >
                          Keep
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setConfirming(s.id)}
                        aria-label={`Delete ${s.title ?? "Untitled chat"}`}
                        className="shrink-0 rounded px-1.5 py-0.5 text-xs text-ink-muted opacity-0 focus:opacity-100 group-hover:opacity-100"
                      >
                        ⋯
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </nav>
      </aside>
    </>
  );
}
