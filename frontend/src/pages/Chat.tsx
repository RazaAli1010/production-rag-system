import { useCallback, useState } from "react";
import type { ApiError } from "../api/errors";
import type { Citation } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { CitationPanel } from "../chat/CitationPanel";
import { Composer } from "../chat/Composer";
import { FlagPicker } from "../chat/FlagPicker";
import { Thread } from "../chat/Thread";
import { useAsk } from "../chat/useAsk";
import { useFlags } from "../chat/useFlags";
import { useLock } from "../chat/useLock";
import { Sidebar } from "../sessions/Sidebar";
import { loadTranscript, useSession, useSessionList } from "../sessions/useSessions";
import { HealthBanner } from "../ui/HealthBanner";
import { Header } from "../ui/Header";

export function Chat() {
  const { user } = useAuth();
  const { sessionId, setSessionId, ensureSession, newSession } = useSession();
  const { sessions, refresh, remove } = useSessionList(Boolean(user));
  const { flags, setFlag, adoptSession, loadFor } = useFlags(sessionId);
  const { turns, busyUntil, busyReason, streaming, ask, retry, load, reset } = useAsk(
    sessionId,
    flags,
  );
  const { locked, note } = useLock(busyUntil, busyReason);

  const [openCitation, setOpenCitation] = useState<{ c: Citation; i: number } | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // NO eager session create on mount. `handleAsk` awaits `ensureSession()` before every ask, so the
  // id and the anonymous cookie are both in place by the time they matter — while creating one up
  // front wrote an EMPTY session row on every page load. Those rows come back from
  // `GET /api/sessions`, sort to the top of the sidebar as "Untitled chat", and open to an empty
  // thread, which is indistinguishable from a broken click.

  const handleAsk = useCallback(
    async (question: string, namespace?: "pu" | "hec") => {
      // Pass the resolved id explicitly: on the very first ask the session was only just created,
      // so `useAsk`'s captured `sessionId` is still null this render.
      const id = await ensureSession();
      adoptSession(id); // bind the current selection to the (possibly just-created) session
      await ask(question, namespace, id);
      if (user) void refresh(); // the list's title/last_active_at just changed
    },
    [adoptSession, ask, ensureSession, refresh, user],
  );

  const openSession = useCallback(
    async (id: string) => {
      // Load BEFORE switching, so a failed fetch leaves the current thread and the sidebar
      // highlight consistent instead of pointing at a session whose transcript never arrived.
      try {
        const turns = await loadTranscript(id);
        setSessionId(id);
        load(turns);
        loadFor(id); // restore the pipeline this conversation was asked with
        setLoadError(null);
      } catch (e) {
        setLoadError(
          (e as ApiError).status === 404
            ? "That conversation is no longer available."
            : "Could not open that conversation. Check your connection and try again.",
        );
      }
    },
    [load, loadFor, setSessionId],
  );

  const startNewChat = useCallback(async () => {
    reset();
    loadFor(null); // back to the defaults, and the picker unlocks with the empty thread
    setLoadError(null);
    // No `ensureSession()` here either — the new server session is created by the first ask.
    // Creating it now would put another empty "Untitled chat" in the sidebar.
    await newSession();
  }, [loadFor, newSession, reset]);

  const handleDelete = useCallback(
    async (id: string) => {
      await remove(id);
      if (id === sessionId) await startNewChat();
    },
    [remove, sessionId, startNewChat],
  );

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar
        sessions={sessions}
        activeId={sessionId}
        authed={Boolean(user)}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onNewChat={() => void startNewChat()}
        onOpenSession={(id) => void openSession(id)}
        onDelete={(id) => void handleDelete(id)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <Header onOpenDrawer={() => setDrawerOpen(true)} />
        <HealthBanner />
        {loadError && (
          <p
            role="alert"
            className="border-b border-flag/30 bg-flag/[0.06] px-4 py-2 text-sm text-flag"
          >
            {loadError}
          </p>
        )}
        <Thread
          turns={turns}
          onOpenCitation={(c, i) => setOpenCitation({ c, i })}
          onRetry={(id) => void retry(id)}
          onPickExample={(q) => setDraft(q)}
        />
        <Composer
          key={draft ?? ""}
          initialValue={draft ?? ""}
          onAsk={(q, ns) => void handleAsk(q, ns)}
          disabled={locked || streaming}
          lockNote={note}
        >
          <FlagPicker flags={flags} onToggle={setFlag} locked={turns.length > 0} />
        </Composer>
      </div>

      <CitationPanel
        citation={openCitation?.c ?? null}
        index={openCitation?.i ?? 0}
        onClose={() => setOpenCitation(null)}
      />
    </div>
  );
}
