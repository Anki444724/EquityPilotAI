"use client";

/**
 * Settings.
 *
 * Every control here is backed by an endpoint that already exists:
 *
 *   Account      GET  /api/v1/auth/me
 *   Password     POST /api/v1/auth/password        + GET /auth/password-policy
 *   Sessions     GET  /api/v1/auth/sessions        + DELETE /auth/sessions
 *   Language     PUT  /api/v1/auth/me/language     + GET /api/v1/ai/languages
 *   Appearance   client-side theme (ThemeProvider)
 *
 * Anything the backend does not implement is shown as unavailable rather than
 * wired to a control that silently does nothing.
 */

import { AppShell } from "@/components/layout/app-shell";
import { useAuth } from "@/components/layout/auth-provider";
import { useTheme } from "@/components/layout/theme-provider";
import { Badge, Card, CardBody, CardHeader, Skeleton } from "@/components/ui";
import { ApiError, aiApi, authApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertCircle, CheckCircle2, LoaderCircle, LogOut, Monitor, Moon, Shield, Sun,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";

const LANGUAGE_KEY = "ierp:ai-language";

export default function SettingsPage() {
  return (
    <AppShell>
      <div className="mx-auto max-w-3xl space-y-5 p-1 sm:p-2">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text)]">Settings</h1>
          <p className="mt-0.5 text-xs text-[var(--text-muted)]">
            Your account, security and application preferences.
          </p>
        </div>

        <AccountSection />
        <SecuritySection />
        <AppearanceSection />
        <PreferencesSection />
        <NotificationsSection />
      </div>
    </AppShell>
  );
}

/* --------------------------------------------------------------- helpers */

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] py-2.5 last:border-0">
      <span className="text-xs text-[var(--text-muted)]">{label}</span>
      <span className="text-right text-[0.8125rem] text-[var(--text)]">{children}</span>
    </div>
  );
}

function Field({
  label, type, value, onChange, autoComplete,
}: {
  label: string; type: string; value: string;
  onChange: (value: string) => void; autoComplete: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-[var(--text-muted)]">{label}</span>
      <input
        type={type}
        value={value}
        autoComplete={autoComplete}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-2 text-sm text-[var(--text)] outline-none focus:border-accent-500"
      />
    </label>
  );
}

function Alert({ tone, children }: { tone: "ok" | "err"; children: ReactNode }) {
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded border p-2.5 text-xs",
        tone === "ok"
          ? "border-gain/40 bg-gain/10 text-gain"
          : "border-loss/40 bg-loss/10 text-loss",
      )}
    >
      {tone === "ok"
        ? <CheckCircle2 size={14} className="mt-px shrink-0" />
        : <AlertCircle size={14} className="mt-px shrink-0" />}
      <span>{children}</span>
    </div>
  );
}

/* --------------------------------------------------------------- account */

function AccountSection() {
  const { user } = useAuth();

  return (
    <Card>
      <CardHeader
        title="Account"
        subtitle="Read-only: these values come from your identity provider."
      />
      <CardBody className="pt-0">
        {!user ? <Skeleton className="h-24 w-full" /> : (
          <div className="flex flex-col gap-1">
            <div className="mb-3 flex items-center gap-3 border-b border-[var(--border)] pb-3">
              <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-accent-500/15 text-sm font-semibold text-accent-500">
                {(user.name ?? user.email ?? "?").slice(0, 1).toUpperCase()}
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-[var(--text)]">{user.name}</div>
                <div className="truncate text-xs text-[var(--text-muted)]">{user.email}</div>
              </div>
            </div>
            <Row label="Role">
              <Badge variant="accent">{user.role}</Badge>
            </Row>
            <Row label="Email verified">
              {user.email_verified
                ? <Badge variant="gain">Verified</Badge>
                : <Badge variant="warn">Not verified</Badge>}
            </Row>
            <Row label="Sign-in method">{user.provider}</Row>
            <Row label="Two-factor authentication">
              {user.mfa_enabled
                ? <Badge variant="gain">Enabled</Badge>
                : <Badge>Disabled</Badge>}
            </Row>
            {user.tenant_name && <Row label="Organisation">{user.tenant_name}</Row>}
            <Row label="User ID">
              <span className="num text-[0.6875rem] text-[var(--text-muted)]">{user.id}</span>
            </Row>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/* -------------------------------------------------------------- security */

function SecuritySection() {
  const { signOut } = useAuth();
  const router = useRouter();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);

  const policy = useQuery({
    queryKey: ["password-policy"],
    queryFn: authApi.passwordPolicy,
    staleTime: 5 * 60 * 1000,
  });

  const sessions = useQuery({
    queryKey: ["auth-sessions"],
    queryFn: authApi.sessions,
    retry: (count, err) => !(err instanceof ApiError && err.status === 401) && count < 1,
  });

  const changePassword = useMutation({
    mutationFn: () => authApi.changePassword(current, next),
    onSuccess: () => {
      setCurrent(""); setNext(""); setConfirm("");
      setError(null);
      setNotice("Password changed. Other sessions were signed out.");
      sessions.refetch();
    },
    onError: (err: unknown) => {
      setNotice(null);
      setError(err instanceof ApiError ? err.message : "Could not change the password.");
    },
  });

  const revokeAll = useMutation({
    mutationFn: authApi.revokeSessions,
    onSuccess: async () => {
      // Revoking every session includes this one: the only honest thing to do
      // next is send the user back to sign-in.
      await signOut();
      router.replace("/");
    },
    onError: (err: unknown) => {
      setError(err instanceof ApiError ? err.message : "Could not revoke sessions.");
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    if (next !== confirm) {
      setError("The new password and its confirmation do not match.");
      return;
    }
    changePassword.mutate();
  }

  async function handleSignOut() {
    setSigningOut(true);
    try {
      await signOut();
    } finally {
      setSigningOut(false);
      router.replace("/");
    }
  }

  return (
    <div id="security" className="scroll-mt-20 space-y-5">
      <Card>
        <CardHeader title="Change password" subtitle={policy.data?.message} />
        <CardBody className="pt-0">
          <form onSubmit={submit} className="space-y-3">
            {notice && <Alert tone="ok">{notice}</Alert>}
            {error && <Alert tone="err">{error}</Alert>}
            <Field label="Current password" type="password" value={current}
                   onChange={setCurrent} autoComplete="current-password" />
            <Field label="New password" type="password" value={next}
                   onChange={setNext} autoComplete="new-password" />
            <Field label="Confirm new password" type="password" value={confirm}
                   onChange={setConfirm} autoComplete="new-password" />
            {policy.data?.requires?.length ? (
              <ul className="space-y-0.5 text-[0.6875rem] text-[var(--text-muted)]">
                {policy.data.requires.map((rule) => <li key={rule}>· {rule}</li>)}
              </ul>
            ) : null}
            <button
              type="submit"
              disabled={changePassword.isPending || !current || !next}
              className="inline-flex min-h-10 items-center gap-2 rounded-md bg-accent-500 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-50"
            >
              {changePassword.isPending && <LoaderCircle size={13} className="animate-spin" />}
              Update password
            </button>
          </form>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Active sessions"
          subtitle="Every device holding a refresh token for this account."
          action={
            <button
              type="button"
              onClick={() => revokeAll.mutate()}
              disabled={revokeAll.isPending}
              className="rounded border border-loss/40 px-2.5 py-1 text-[0.6875rem] font-medium text-loss transition-colors hover:bg-loss/10 disabled:opacity-50"
            >
              {revokeAll.isPending ? "Revoking…" : "Revoke all"}
            </button>
          }
        />
        <CardBody className="pt-0">
          {sessions.isLoading && <Skeleton className="h-16 w-full" />}
          {sessions.isError && (
            <Alert tone="err">
              {sessions.error instanceof ApiError && sessions.error.status === 401
                ? "Your session has expired. Sign in again to see your devices."
                : "Active sessions are unavailable right now."}
            </Alert>
          )}
          {sessions.data?.length === 0 && (
            <p className="text-xs text-[var(--text-muted)]">No other active sessions.</p>
          )}
          {sessions.data && sessions.data.length > 0 && (
            <ul className="space-y-2">
              {sessions.data.map((session) => (
                <li
                  key={session.session_id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded border border-[var(--border)] px-3 py-2"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Monitor size={13} className="shrink-0 text-[var(--text-muted)]" />
                      <span className="truncate text-xs text-[var(--text)]">
                        {session.user_agent ?? "Unknown device"}
                      </span>
                      {session.current && <Badge variant="gain">This device</Badge>}
                    </div>
                    <div className="mt-0.5 text-[0.6875rem] text-[var(--text-muted)]">
                      {session.ip_address ?? "IP not recorded"}
                      {session.expires_at && ` · expires ${new Date(session.expires_at).toLocaleString()}`}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-[0.6875rem] text-[var(--text-muted)]">
            Revoking signs out every device, including this one.
          </p>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Sign out" subtitle="End the session on this device only." />
        <CardBody className="pt-0">
          <button
            type="button"
            onClick={handleSignOut}
            disabled={signingOut}
            className="inline-flex min-h-10 items-center gap-2 rounded-md border border-loss/40 px-3 py-2 text-xs font-medium text-loss transition-colors hover:bg-loss/10 disabled:opacity-60"
          >
            {signingOut
              ? <LoaderCircle size={13} className="animate-spin" />
              : <LogOut size={13} />}
            Log out
          </button>
        </CardBody>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------ appearance */

function AppearanceSection() {
  const { theme, toggle } = useTheme();

  return (
    <Card>
      <CardHeader title="Appearance" subtitle="Stored in this browser." />
      <CardBody className="pt-0">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-[0.8125rem] text-[var(--text)]">Theme</div>
            <div className="text-[0.6875rem] text-[var(--text-muted)]">
              Currently {theme === "dark" ? "dark" : "light"}.
            </div>
          </div>
          <button
            type="button"
            onClick={toggle}
            className="inline-flex min-h-10 items-center gap-2 rounded-md border border-[var(--border)] px-3 py-2 text-xs text-[var(--text)] transition-colors hover:bg-[var(--bg-subtle)]"
          >
            {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
            Switch to {theme === "dark" ? "light" : "dark"}
          </button>
        </div>
      </CardBody>
    </Card>
  );
}

/* ----------------------------------------------------------- preferences */

function PreferencesSection() {
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [language, setLanguage] = useState<string>(() => {
    if (typeof window === "undefined") return "auto";
    try {
      return window.localStorage.getItem(LANGUAGE_KEY) ?? "auto";
    } catch {
      return "auto";
    }
  });

  const languages = useQuery({
    queryKey: ["ai-languages"],
    queryFn: aiApi.languages,
    staleTime: 10 * 60 * 1000,
    retry: (count, err) => !(err instanceof ApiError && err.status === 401) && count < 1,
  });

  const save = useMutation({
    mutationFn: (value: string) => aiApi.saveLanguage(value),
    onSuccess: (_data, value) => {
      setError(null);
      setNotice("Language preference saved.");
      try { window.localStorage.setItem(LANGUAGE_KEY, value); } catch { /* private mode */ }
    },
    onError: (err: unknown) => {
      setNotice(null);
      setError(err instanceof ApiError ? err.message : "Could not save the preference.");
    },
  });

  return (
    <Card>
      <CardHeader
        title="Preferences"
        subtitle="Applies to AI research answers and generated reports."
      />
      <CardBody className="space-y-3 pt-0">
        {notice && <Alert tone="ok">{notice}</Alert>}
        {error && <Alert tone="err">{error}</Alert>}
        <label className="block">
          <span className="mb-1 block text-xs text-[var(--text-muted)]">
            AI response language
          </span>
          <select
            value={language}
            disabled={languages.isLoading || save.isPending}
            onChange={(event) => {
              setLanguage(event.target.value);
              save.mutate(event.target.value);
            }}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-2 text-sm text-[var(--text)] outline-none focus:border-accent-500 disabled:opacity-50"
          >
            <option value="auto">Match the language of my question</option>
            {(languages.data?.languages ?? []).map((spec) => (
              <option key={spec.code} value={spec.code}>{spec.label}</option>
            ))}
          </select>
        </label>
        {languages.isError && (
          <p className="text-[0.6875rem] text-[var(--text-muted)]">
            The language list could not be loaded; the saved preference is unchanged.
          </p>
        )}
      </CardBody>
    </Card>
  );
}

/* ---------------------------------------------------------- notifications */

function NotificationsSection() {
  return (
    <Card>
      <CardHeader title="Notifications" subtitle="Not available yet." />
      <CardBody className="pt-0">
        <div className="flex items-start gap-2 rounded border border-[var(--border)] bg-[var(--bg-subtle)] p-3">
          <Shield size={14} className="mt-px shrink-0 text-[var(--text-muted)]" />
          <div className="text-xs text-[var(--text-muted)]">
            <p className="text-[var(--text)]">No delivery preferences to configure.</p>
            <p className="mt-1">
              Portfolio alerts are evaluated and shown inside Portfolio Intelligence.
              The backend exposes no per-user notification settings, so nothing is
              offered here rather than presenting switches that would not be read.
            </p>
          </div>
        </div>
      </CardBody>
    </Card>
  );
}
