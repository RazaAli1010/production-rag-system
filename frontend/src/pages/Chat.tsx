import { useCallback, useEffect, useState } from "react";
import type { Citation } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { CitationPanel } from "../chat/CitationPanel";
import { Composer } from "../chat/Composer";
import { Thread } from "../chat/Thread";
import { useAsk } from "../chat/useAsk";
import { useLock } from "../chat/useLock";
import { Sidebar } from "../sessions/Sidebar";
import { loadTranscript, useSession, useSessionList } from "../sessions/useSessions";
import { HealthBanner } from "../ui/HealthBanner";
import { Header } from "../ui/Header";

export function Chat() {
  const { user } = useAuth();
  const { sessionId, setSessionId, ensureSession, newSession } = useSession();
  const { sessions, refresh, remove } = useSessionList(Boolean(user));
  const { turns, busyUntil, busyReason, streaming, ask, retry, load, reset } = useAsk(sessionId);
  const { locked, note } = useLock(busyUntil, busyReason);

  const [openCitation, setOpenCitation] = useState<{ c: Citation; i: number } | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);

  // Create the session up front so the first ask already carries a session_id and the anonymous
  // cookie is in place before it matters.
  useEffect(() => {
    void ensureSession();
  }, [ensureSession]);

  const handleAsk = useCallback(
    async (question: string, namespace?: "pu" | "hec") => {
      await ensureSession();
      await ask(question, namespace);
      if (user) void refresh(); // the list's title/last_active_at just changed
    },
    [ask, ensureSession, refresh, user],
  );

  const openSession = useCallback(
    async (id: string) => {
      setSessionId(id);
      load(await loadTranscript(id));
    },
    [load, setSessionId],
  );

  const startNewChat = useCallback(async () => {
    reset();
    await newSession();
    await ensureSession();
  }, [ensureSession, newSession, reset]);

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
        />
      </div>

      <CitationPanel
        citation={openCitation?.c ?? null}
        index={openCitation?.i ?? 0}
        onClose={() => setOpenCitation(null)}
      />
    </div>
  );
}
