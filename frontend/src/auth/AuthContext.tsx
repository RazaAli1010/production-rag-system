import { createContext, useCallback, useContext, useEffect, useMemo, useReducer } from "react";
import { fetchJson, setLogoutHandler } from "../api/client";
import { clearTokens, getRefreshToken, setTokens } from "../api/tokens";
import type { TokenResponse, UserOut } from "../api/types";

interface State {
  user: UserOut | null;
  /** True until the initial "am I still logged in?" probe settles. */
  loading: boolean;
}

type Action = { t: "user"; user: UserOut | null } | { t: "settled" };

function reducer(state: State, a: Action): State {
  switch (a.t) {
    case "user":
      return { ...state, user: a.user, loading: false };
    case "settled":
      return { ...state, loading: false };
  }
}

interface AuthValue extends State {
  isAdmin: boolean;
  login(email: string, password: string): Promise<void>;
  register(email: string, password: string): Promise<void>;
  logout(): Promise<void>;
}

const Ctx = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, { user: null, loading: true });

  const loadMe = useCallback(async () => {
    try {
      dispatch({ t: "user", user: await fetchJson<UserOut>("/api/auth/me") });
    } catch {
      dispatch({ t: "user", user: null });
    }
  }, []);

  // A reload leaves the access token gone (memory-only) but the refresh token present. One
  // /me call triggers the client's silent refresh and restores the session — or fails and we
  // stay anonymous, which is a perfectly good outcome.
  useEffect(() => {
    if (!getRefreshToken()) {
      dispatch({ t: "settled" });
      return;
    }
    void loadMe();
  }, [loadMe]);

  // A refresh failure inside the client must tear down app state too, not just the tokens.
  useEffect(() => {
    setLogoutHandler(() => dispatch({ t: "user", user: null }));
    return () => setLogoutHandler(null);
  }, []);

  const login = useCallback(
    async (email: string, password: string) => {
      // OAuth2 password flow: `/api/auth/token` takes a FORM body with a `username` field
      // (fastapi.security.OAuth2PasswordRequestForm), not JSON and not `email`.
      const form = new URLSearchParams({ username: email, password });
      const res = await fetchJson<TokenResponse>("/api/auth/token", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: form.toString(),
      });
      setTokens(res.access_token, res.refresh_token);
      await loadMe();
    },
    [loadMe],
  );

  const register = useCallback(
    async (email: string, password: string) => {
      await fetchJson<UserOut>("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      await login(email, password); // AC-36: registration lands the user in the chat, logged in
    },
    [login],
  );

  const logout = useCallback(async () => {
    try {
      await fetchJson<void>("/api/auth/logout", { method: "POST" });
    } catch {
      /* the local teardown matters more than the server ack */
    }
    clearTokens();
    dispatch({ t: "user", user: null });
  }, []);

  const value = useMemo<AuthValue>(
    () => ({ ...state, isAdmin: state.user?.role === "admin", login, register, logout }),
    [state, login, register, logout],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used inside AuthProvider");
  return v;
}
