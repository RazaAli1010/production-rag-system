import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function Header({ onOpenDrawer }: { onOpenDrawer?: () => void }) {
  const { user, isAdmin, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <header className="flex items-center gap-3 border-b border-rule bg-paper px-4 py-2.5">
      {onOpenDrawer && (
        <button
          type="button"
          onClick={onOpenDrawer}
          aria-label="Open your chats"
          className="rounded px-2 py-1 text-lg leading-none lg:hidden"
        >
          ☰
        </button>
      )}

      <Link to="/" className="font-display text-lg font-bold tracking-tight">
        CampusRAG
      </Link>

      <nav aria-label="Site" className="ml-auto flex items-center gap-3 text-sm">
        <Link to="/sources" className="text-ink-muted hover:text-ink">
          Sources
        </Link>
        {isAdmin && (
          <Link to="/admin" className="text-ink-muted hover:text-ink">
            Stats
          </Link>
        )}
        {user ? (
          <button
            type="button"
            onClick={() => void logout().then(() => navigate("/"))}
            className="text-ink-muted hover:text-ink"
          >
            Log out
          </button>
        ) : (
          <Link to="/login" className="font-medium text-seal">
            Log in
          </Link>
        )}
      </nav>
    </header>
  );
}
