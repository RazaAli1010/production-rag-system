import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { fetchJson } from "../api/client";
import type { ApiError } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { PASSWORD_RULES, passwordIssues } from "../auth/passwordRules";
import { Header } from "../ui/Header";

const FIELD =
  "mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-base";
const BUTTON =
  "w-full rounded bg-seal px-4 py-2 font-medium text-white disabled:opacity-50";
const LINK = "text-seal underline underline-offset-2";

function AuthShell({ title, blurb, children }: { title: string; blurb: string; children: React.ReactNode }) {
  return (
    <div className="flex h-dvh flex-col">
      <Header />
      <main className="mx-auto w-full max-w-sm px-4 py-10">
        <h1 className="font-display text-xl font-bold">{title}</h1>
        <p className="mt-2 text-sm text-ink-muted">{blurb}</p>
        {children}
      </main>
    </div>
  );
}

function Alert({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="rounded border border-flag/40 bg-flag/[0.07] px-3 py-2 text-sm">
      {children}
    </p>
  );
}

/** The live policy checklist. Shown wherever a NEW password is being chosen. */
function PasswordRules({ password, email }: { password: string; email: string }) {
  const local = email.split("@")[0]!.toLowerCase();
  return (
    <ul className="mt-2 space-y-0.5 text-xs text-ink-muted">
      {PASSWORD_RULES.map((rule) => {
        const met = rule.ok(password, local);
        return (
          <li key={rule.label} className={met ? "text-seal" : undefined}>
            <span aria-hidden="true">{met ? "✓" : "·"}</span> <span>{rule.label}</span>
            {met && <span className="sr-only"> — met</span>}
          </li>
        );
      })}
    </ul>
  );
}

/** A 422 carries the useful string per field; `message` is only "Invalid request" there. */
const readError = (err: unknown) => {
  const e = err as ApiError;
  return e.fields?.[0]?.msg ?? e.message;
};

/** T18 — login and register share a form; the only differences are the verb and the copy. */
function AuthForm({ mode }: { mode: "login" | "register" }) {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isRegister = mode === "register";
  const weak = isRegister && passwordIssues(password, email).length > 0;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await (isRegister ? register(email, password) : login(email, password));
      navigate("/");
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell
      title={isRegister ? "Create an account" : "Log in"}
      blurb={
        isRegister
          ? "An account keeps your chats and lets you pick them up later."
          : "Your saved chats are waiting."
      }
    >
      <>
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
              className={FIELD}
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
              autoComplete={isRegister ? "new-password" : "current-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={FIELD}
            />
            {isRegister && <PasswordRules password={password} email={email} />}
          </div>

          {error && <Alert>{error}</Alert>}

          <button type="submit" disabled={busy || weak} className={BUTTON}>
            {isRegister ? "Create account" : "Log in"}
          </button>
        </form>

        <p className="mt-6 text-sm text-ink-muted">
          {isRegister ? (
            <>
              Already have an account?{" "}
              <Link to="/login" className={LINK}>
                Log in
              </Link>
            </>
          ) : (
            <>
              No account?{" "}
              <Link to="/register" className={LINK}>
                Create one
              </Link>
              {" · "}
              <Link to="/forgot-password" className={LINK}>
                Forgot password?
              </Link>
            </>
          )}
        </p>
      </>
    </AuthShell>
  );
}

export function Login() {
  return <AuthForm mode="login" />;
}

export function Register() {
  return <AuthForm mode="register" />;
}

/** Step 1 of a reset. The answer is the same whether or not the address exists. */
export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await fetchJson<void>("/api/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
    } catch {
      // Deliberately swallowed: showing a failure here would tell the sender which addresses
      // exist. The server already answers 202 for every outcome.
    }
    setSent(true);
    setBusy(false);
  };

  return (
    <AuthShell
      title="Reset your password"
      blurb="We'll email you a link to choose a new one."
    >
      {sent ? (
        <>
          <p className="mt-6 rounded border border-rule bg-paper-raised px-3 py-2 text-sm">
            If an account exists for {email}, a reset link is on its way. It expires in 30 minutes.
          </p>
          <p className="mt-6 text-sm text-ink-muted">
            <Link to="/login" className={LINK}>
              Back to log in
            </Link>
          </p>
        </>
      ) : (
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
              className={FIELD}
            />
          </div>
          <button type="submit" disabled={busy} className={BUTTON}>
            Send reset link
          </button>
          <p className="text-sm text-ink-muted">
            <Link to="/login" className={LINK}>
              Back to log in
            </Link>
          </p>
        </form>
      )}
    </AuthShell>
  );
}

/** Step 2: the link lands here with `?token=`. */
export function ResetPassword() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await fetchJson<void>("/api/auth/reset-password", {
        method: "POST",
        body: JSON.stringify({ token, password }),
      });
      // Every session was revoked server-side, so there is nothing to do but log in again.
      navigate("/login");
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <AuthShell title="Choose a new password" blurb="This link is incomplete.">
        <div className="mt-6 space-y-4">
          <Alert>That reset link is missing its token. Request a new one.</Alert>
          <p className="text-sm text-ink-muted">
            <Link to="/forgot-password" className={LINK}>
              Send a new link
            </Link>
          </p>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Choose a new password"
      blurb="You'll be logged out everywhere else once it changes."
    >
      <form onSubmit={submit} className="mt-6 space-y-4">
        <div>
          <label htmlFor="password" className="block text-sm font-medium">
            New password
          </label>
          <input
            id="password"
            type="password"
            required
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={FIELD}
          />
          <PasswordRules password={password} email="" />
        </div>

        {error && <Alert>{error}</Alert>}

        <button
          type="submit"
          disabled={busy || passwordIssues(password).length > 0}
          className={BUTTON}
        >
          Set new password
        </button>
        <p className="text-sm text-ink-muted">
          <Link to="/forgot-password" className={LINK}>
            Request a new link
          </Link>
        </p>
      </form>
    </AuthShell>
  );
}
