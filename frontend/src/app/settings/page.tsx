"use client";

/**
 * Account settings — profile, password, sessions.
 *
 * Talks only to the existing auth endpoints (`/auth/me`, `/auth/password`,
 * `/auth/sessions`). No new identity system.
 */

import { AppShell } from "@/components/layout/app-shell";
import { useAuth } from "@/components/layout/auth-provider";
import { Badge, Card, CardBody, CardHeader } from "@/components/ui";
import { ApiError, authApi } from "@/lib/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

export default function SettingsPage() {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const me = useQuery({ queryKey: ["me-full"], queryFn: authApi.me });
  const sessions = useQuery({ queryKey: ["auth-sessions"], queryFn: authApi.sessions });
  const profile = me.data ?? user;

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  const changePassword = useMutation({
    mutationFn: () => authApi.changePassword(currentPassword, newPassword),
    onSuccess: (result) => {
      setPasswordError(null);
      setPasswordMessage(result.message || "Password updated.");
      setCurrentPassword("");
      setNewPassword("");
      setConfirm("");
    },
    onError: (error) => {
      setPasswordMessage(null);
      setPasswordError(error instanceof ApiError ? error.message : "Could not update password.");
    },
  });

  const revokeOthers = useMutation({
    mutationFn: authApi.revokeSessions,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["auth-sessions"] }),
  });

  return (
    <AppShell>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Your account, password and signed-in devices.
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader title="Profile" subtitle="From the current session" />
          <CardBody className="space-y-2 text-sm">
            <Row label="Name" value={profile?.name || "—"} />
            <Row label="Email" value={profile?.email || "—"} />
            <Row
              label="Role"
              value={profile?.role ? <Badge>{profile.role.replace(/_/g, " ")}</Badge> : "—"}
            />
            <Row label="Organisation" value={profile?.tenant_name || "—"} />
            {profile?.is_dev_identity && (
              <p className="rounded border border-warn/40 bg-warn/10 px-2 py-1.5 text-xs text-warn">
                Development identity is active. Set NATIVE_AUTH=true for real sign-in.
              </p>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Password" subtitle="POST /api/v1/auth/password" />
          <CardBody>
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                if (newPassword !== confirm) {
                  setPasswordError("New password and confirmation do not match.");
                  return;
                }
                changePassword.mutate();
              }}
            >
              <Field
                label="Current password"
                type="password"
                value={currentPassword}
                onChange={setCurrentPassword}
                autoComplete="current-password"
              />
              <Field
                label="New password"
                type="password"
                value={newPassword}
                onChange={setNewPassword}
                autoComplete="new-password"
              />
              <Field
                label="Confirm new password"
                type="password"
                value={confirm}
                onChange={setConfirm}
                autoComplete="new-password"
              />
              {passwordError && <p className="text-xs text-loss">{passwordError}</p>}
              {passwordMessage && <p className="text-xs text-gain">{passwordMessage}</p>}
              <button
                type="submit"
                disabled={changePassword.isPending || !currentPassword || !newPassword}
                className="rounded-md bg-accent-500 px-3 py-2 text-xs font-medium text-white disabled:opacity-50"
              >
                {changePassword.isPending ? "Updating…" : "Update password"}
              </button>
            </form>
          </CardBody>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader
            title="Sessions"
            subtitle="Devices signed in with this account"
            action={
              <button
                type="button"
                onClick={() => revokeOthers.mutate()}
                disabled={revokeOthers.isPending}
                className="text-xs text-loss hover:underline disabled:opacity-50"
              >
                Sign out other devices
              </button>
            }
          />
          <CardBody className="space-y-2">
            {(sessions.data ?? []).length === 0 && (
              <p className="text-xs text-[var(--text-muted)]">No sessions listed.</p>
            )}
            {(sessions.data ?? []).map((session) => (
              <div
                key={session.session_id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[var(--border)] px-3 py-2 text-xs"
              >
                <div className="min-w-0">
                  <div className="truncate text-[var(--text)]">
                    {session.user_agent || "Unknown device"}
                  </div>
                  <div className="text-[var(--text-muted)]">
                    {session.ip_address || "IP unknown"}
                    {session.issued_at ? ` · since ${new Date(session.issued_at).toLocaleString()}` : ""}
                  </div>
                </div>
                {session.current && <Badge variant="accent">This device</Badge>}
              </div>
            ))}
          </CardBody>
        </Card>
      </div>
    </AppShell>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-[var(--border)]/60 py-1.5 last:border-0">
      <span className="text-xs text-[var(--text-muted)]">{label}</span>
      <span className="min-w-0 truncate text-right">{value}</span>
    </div>
  );
}

function Field({
  label, type, value, onChange, autoComplete,
}: {
  label: string; type: string; value: string;
  onChange: (value: string) => void; autoComplete?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[0.6875rem] uppercase tracking-wide text-[var(--text-muted)]">
        {label}
      </span>
      <input
        type={type}
        value={value}
        autoComplete={autoComplete}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-md border border-[var(--border)] bg-[var(--bg)] px-2.5 py-2 text-sm outline-none focus:border-accent-500"
      />
    </label>
  );
}
