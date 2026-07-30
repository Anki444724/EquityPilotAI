"use client";

/**
 * Sign-in screen.
 *
 * The platform previously had none: the frontend was developed against a
 * backend running AUTH_DEV_MODE=true, where every caller is treated as a
 * super admin, so no credentials were ever needed. Against the production
 * backend every request returned 401 and the dashboard reported it as
 * "cannot reach the API".
 */

import { useAuth } from "./auth-provider";
import { ApiError } from "@/lib/api";
import { AlertCircle, LoaderCircle, LockKeyhole } from "lucide-react";
import { useState, type FormEvent } from "react";

export function SignIn() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email.trim(), password);
    } catch (err) {
      // Distinguish "wrong password" from "the API is unreachable" — the two
      // demand completely different actions from the user, and conflating
      // them is the very bug this screen was written to fix.
      if (err instanceof ApiError) {
        setError(
          err.status === 401 ? "Incorrect email or password."
          : err.status === 429 ? "Too many attempts. Try again shortly."
          : `Sign-in failed (HTTP ${err.status}): ${err.message}`,
        );
      } else {
        setError(
          "Cannot reach the API. Check your connection and that the backend is running.",
        );
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--bg)] px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-lg bg-[var(--accent)]">
            <LockKeyhole size={18} className="text-white" />
          </div>
          <h1 className="text-base font-semibold">Sign in</h1>
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
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Email</span>
            <input
              type="email"
              required
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="you@firm.com"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Password</span>
            <input
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--accent)]"
              placeholder="••••••••••"
            />
          </label>

          <button
            type="submit"
            disabled={busy}
            className="flex w-full items-center justify-center gap-2 rounded bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
          >
            {busy && <LoaderCircle size={14} className="animate-spin" />}
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
