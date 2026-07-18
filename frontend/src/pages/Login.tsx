import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { ApiError } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { Header } from "../ui/Header";

/** T18 — login and register share a form; the only differences are the verb and the copy. */
function AuthForm({ mode }: { mode: "login" | "register" }) {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isRegister = mode === "register";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await (isRegister ? register(email, password) : login(email, password));
      navigate("/");
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-dvh flex-col">
      <Header />
      <main className="mx-auto w-full max-w-sm px-4 py-10">
        <h1 className="font-display text-xl font-bold">
          {isRegister ? "Create an account" : "Log in"}
        </h1>
        <p className="mt-2 text-sm text-ink-muted">
          {isRegister
            ? "An account keeps your chats and lets you pick them up later."
            : "Your saved chats are waiting."}
        </p>

        <form onSubmit={submit} className="mt-6 space-y-4">
          <div>
            <label htmlFor="email" className="block text-sm font-medium">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-base"
            />
          </div>
          <div>
            <label htmlFor="password" className="block text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              minLength={8}
              autoComplete={isRegister ? "new-password" : "current-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-base"
            />
            {isRegister && (
              <p className="mt-1 text-xs text-ink-muted">At least 8 characters.</p>
            )}
          </div>

          {error && (
            <p role="alert" className="rounded border border-flag/40 bg-flag/[0.07] px-3 py-2 text-sm">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full rounded bg-seal px-4 py-2 font-medium text-white disabled:opacity-50"
          >
            {isRegister ? "Create account" : "Log in"}
          </button>
        </form>

        <p className="mt-6 text-sm text-ink-muted">
          {isRegister ? (
            <>
              Already have an account?{" "}
              <Link to="/login" className="text-seal underline underline-offset-2">
                Log in
              </Link>
            </>
          ) : (
            <>
              No account?{" "}
              <Link to="/register" className="text-seal underline underline-offset-2">
                Create one
              </Link>
            </>
          )}
        </p>
      </main>
    </div>
  );
}

export function Login() {
  return <AuthForm mode="login" />;
}

export function Register() {
  return <AuthForm mode="register" />;
}
