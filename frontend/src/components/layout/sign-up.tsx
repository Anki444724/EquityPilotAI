"use client";

/**
 * Signup form.
 *
 * Validation is duplicated here and on the server on purpose: the browser
 * copy is a convenience that gives immediate feedback, and the server copy is
 * the control. `/auth/register` is reachable without this form.
 */

import { ApiError, authApi } from "@/lib/api";
import { AlertCircle, Check, LoaderCircle, UserPlus, X } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

interface Props {
  onSignedUp: (message: string) => void;
  onCancel: () => void;
}

/** Mirrors `DEFAULT_PASSWORD_POLICY` on the server. */
function passwordProblems(password: string, email: string): string[] {
  const problems: string[] = [];
  if (password.length < 10) problems.push("At least 10 characters");
  if (!/[a-z]/.test(password)) problems.push("A lower-case letter");
  if (!/[A-Z]/.test(password)) problems.push("An upper-case letter");
  if (!/[0-9]/.test(password)) problems.push("A digit");
  if (!/[^A-Za-z0-9]/.test(password)) problems.push("A symbol");
  const local = email.split("@")[0]?.toLowerCase();
  if (local && local.length > 2 && password.toLowerCase().includes(local)) {
    problems.push("Must not contain your email name");
  }
  return problems;
}

export function SignUp({ onSignedUp, onCancel }: Props) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [handleState, setHandleState] = useState<
    { checking: boolean; available: boolean | null; problems: string[] }
  >({ checking: false, available: null, problems: [] });

  // Debounced availability check, so the user learns a handle is taken while
  // filling the form rather than on submit.
  useEffect(() => {
    if (username.trim().length < 3) {
      setHandleState({ checking: false, available: null, problems: [] });
      return;
    }
    setHandleState((s) => ({ ...s, checking: true }));
    const timer = setTimeout(async () => {
      try {
        const result = await authApi.usernameAvailable(username.trim());
        setHandleState({
          checking: false, available: result.available, problems: result.problems,
        });
      } catch {
        setHandleState({ checking: false, available: null, problems: [] });
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [username]);

  const pwProblems = password ? passwordProblems(password, email) : [];
  const mismatch = confirm.length > 0 && confirm !== password;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (pwProblems.length) {
      setError(`Password needs: ${pwProblems.join(", ").toLowerCase()}.`);
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (handleState.available === false) {
      setError(handleState.problems[0] ?? "That username is not available.");
      return;
    }

    setBusy(true);
    try {
      const result = await authApi.register({
        name: name.trim(), email: email.trim(),
        username: username.trim() || undefined,
        password, confirm_password: confirm,
      });
      onSignedUp(
        result.message ??
          "Account created. Check your email to verify the address before signing in.",
      );
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 429
            ? "Too many attempts. Try again shortly."
            : err.message,
        );
      } else {
        setError("Cannot reach the API. Check your connection.");
      }
    } finally {
      setBusy(false);
    }
  }

  const field =
    "w-full rounded border border-[var(--border)] bg-[var(--bg)] px-2.5 py-1.5 " +
    "text-sm outline-none focus:border-[var(--accent)]";

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--bg)] px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-lg bg-[var(--accent)]">
            <UserPlus size={18} className="text-white" />
          </div>
          <h1 className="text-base font-semibold">Create an account</h1>
          <p className="mt-1 text-xs text-[var(--text-muted)]">
            Institutional Equity Research Platform
          </p>
        </div>

        <form
          onSubmit={onSubmit}
          className="space-y-3 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-5"
        >
          {error && (
            <div className="flex items-start gap-2 rounded border border-loss/40 bg-loss/10 p-2.5 text-xs text-loss">
              <AlertCircle size={14} className="mt-px shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Full name</span>
            <input required value={name} onChange={(e) => setName(e.target.value)}
                   className={field} placeholder="Ankit Singh" autoComplete="name" />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Email</span>
            <input required type="email" value={email} autoComplete="email"
                   onChange={(e) => setEmail(e.target.value)}
                   className={field} placeholder="you@firm.com" />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Username</span>
            <div className="relative">
              <input value={username} onChange={(e) => setUsername(e.target.value)}
                     className={field} placeholder="ankitsingh" autoComplete="username" />
              {username.trim().length >= 3 && (
                <span className="absolute right-2 top-1/2 -translate-y-1/2">
                  {handleState.checking
                    ? <LoaderCircle size={13} className="animate-spin text-[var(--text-muted)]" />
                    : handleState.available === true
                      ? <Check size={13} className="text-gain" />
                      : handleState.available === false
                        ? <X size={13} className="text-loss" />
                        : null}
                </span>
              )}
            </div>
            {handleState.available === false && handleState.problems[0] && (
              <span className="mt-1 block text-[0.6875rem] text-loss">
                {handleState.problems[0]}
              </span>
            )}
          </label>

          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Password</span>
            <input required type="password" value={password} autoComplete="new-password"
                   onChange={(e) => setPassword(e.target.value)} className={field} />
            {password && pwProblems.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {pwProblems.map((p) => (
                  <li key={p} className="text-[0.6875rem] text-[var(--text-muted)]">
                    <X size={9} className="mr-1 inline text-loss" />{p}
                  </li>
                ))}
              </ul>
            )}
            {password && pwProblems.length === 0 && (
              <span className="mt-1 block text-[0.6875rem] text-gain">
                <Check size={9} className="mr-1 inline" />Password meets the policy
              </span>
            )}
          </label>

          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Confirm password</span>
            <input required type="password" value={confirm} autoComplete="new-password"
                   onChange={(e) => setConfirm(e.target.value)} className={field} />
            {mismatch && (
              <span className="mt-1 block text-[0.6875rem] text-loss">
                Passwords do not match.
              </span>
            )}
          </label>

          <button type="submit" disabled={busy}
                  className="flex w-full items-center justify-center gap-2 rounded bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:opacity-60">
            {busy && <LoaderCircle size={14} className="animate-spin" />}
            {busy ? "Creating account…" : "Create account"}
          </button>

          <button type="button" onClick={onCancel}
                  className="w-full text-center text-xs text-[var(--text-muted)] hover:text-[var(--text)]">
            Already have an account? Sign in
          </button>
        </form>
      </div>
    </div>
  );
}
