"use client";

/**
 * Phase 7 — Enterprise User Management & Subscription Center.
 *
 * Users (add/edit/suspend/ban/restore/delete), roles & permissions,
 * subscriptions (upgrade/downgrade/renew/extend), payments (invoice/refund),
 * sessions (active devices, logout all, force logout), security (2FA, reset,
 * verification, login & IP history), notifications and analytics.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, Bell, CreditCard, KeyRound, Layers, Shield, UserPlus, Users as UsersIcon,
} from "lucide-react";

import { userCenterApi } from "@/lib/api";
import type { AdminUser, RoleInfo, UserInvoice, UserSession, UserSubscription } from "@/lib/types";
import { Button, Pager, Select, StatusPill, TextInput, formatWhen } from "./primitives";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";

type Tab = "users" | "subscriptions" | "payments" | "sessions" | "security" | "notifications" | "permissions" | "analytics";

const TABS: { key: Tab; label: string; icon: typeof UsersIcon }[] = [
  { key: "users", label: "Users", icon: UsersIcon },
  { key: "subscriptions", label: "Subscriptions", icon: KeyRound },
  { key: "payments", label: "Payments", icon: CreditCard },
  { key: "sessions", label: "Sessions", icon: Activity },
  { key: "security", label: "Security", icon: Shield },
  { key: "notifications", label: "Notifications", icon: Bell },
  { key: "permissions", label: "Roles & Permissions", icon: Layers },
  { key: "analytics", label: "Analytics", icon: Activity },
];

export default function UsersView() {
  const [tab, setTab] = useState<Tab>("users");
  return (
    <div className="space-y-3">
      <div className="flex gap-1 overflow-x-auto pb-1">
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button key={t.key} onClick={() => setTab(t.key)}
                    className={tab === t.key
                      ? "flex shrink-0 items-center gap-1.5 rounded bg-accent-500/10 px-3 py-1.5 text-xs font-medium text-accent-500"
                      : "flex shrink-0 items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--text)]"}>
              <Icon className="h-3.5 w-3.5" /> {t.label}
            </button>
          );
        })}
      </div>
      {tab === "users" && <UsersTab />}
      {tab === "subscriptions" && <SubscriptionsTab />}
      {tab === "payments" && <PaymentsTab />}
      {tab === "sessions" && <SessionsTab />}
      {tab === "security" && <SecurityTab />}
      {tab === "notifications" && <NotificationsTab />}
      {tab === "permissions" && <PermissionsTab />}
      {tab === "analytics" && <AnalyticsTab />}
    </div>
  );
}

const ROLE_LABELS: Record<string, string> = {
  super_admin: "Super Admin", admin: "Admin", analyst: "Analyst",
  researcher: "Researcher", subscriber: "Premium", read_only: "Free", guest: "Guest",
};

function UserRow({ user, onSelect }: { user: AdminUser; onSelect: (u: AdminUser) => void }) {
  return (
    <tr className="border-t border-[var(--border)]">
      <td className="px-3 py-2">
        <button type="button" onClick={() => onSelect(user)} className="text-left font-medium text-accent-500 hover:underline">
          {user.name}
        </button>
        <div className="text-[0.625rem] text-[var(--text-muted)]">{user.email}</div>
      </td>
      <td className="px-3 py-2">{ROLE_LABELS[user.role] ?? user.role}</td>
      <td className="px-3 py-2"><StatusPill status={user.status} /></td>
      <td className="px-3 py-2 text-[var(--text-muted)]">{user.mfa_method !== "none" ? "2FA ✓" : "—"}</td>
      <td className="px-3 py-2 text-[var(--text-muted)]">{user.last_login_at ? formatWhen(user.last_login_at) : "—"}</td>
    </tr>
  );
}

/* ==================================================================== */
function UsersTab() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [selected, setSelected] = useState<AdminUser | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["admin", "users", page, role, status, search],
    queryFn: () => userCenterApi.list({ page, page_size: 25, role, status, search }),
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "users"] });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }} placeholder="Search name / email…" className="max-w-xs" />
        <Select value={role} onChange={(v) => { setRole(v); setPage(1); }}
                options={[{ value: "", label: "All roles" }, ...Object.entries(ROLE_LABELS).map(([k, l]) => ({ value: k, label: l }))]} />
        <Select value={status} onChange={(v) => { setStatus(v); setPage(1); }}
                options={[{ value: "", label: "All statuses" }, { value: "active", label: "Active" }, { value: "suspended", label: "Suspended" }, { value: "disabled", label: "Banned" }, { value: "pending", label: "Pending" }]} />
        <div className="flex-1" />
        <Button variant="primary" onClick={() => setShowForm(true)}><UserPlus className="h-3.5 w-3.5" /> Add user</Button>
      </div>

      {showForm && <AddUserForm onClose={() => setShowForm(false)} onDone={invalidate} />}

      <Card>
        {isLoading ? <Skeleton className="h-64" /> : (
          <table className="w-full text-xs">
            <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">User</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Role</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Status</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">2FA</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Last login</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Actions</th></tr></thead>
            <tbody>
              {(data?.items ?? []).map((u) => (
                <UserRow key={u.id} user={u} onSelect={setSelected} />
              ))}
              {(data?.items ?? []).length === 0 && <tr><td colSpan={6} className="py-4 text-center text-[var(--text-muted)]">No users.</td></tr>}
            </tbody>
          </table>
        )}
        <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
      </Card>

      {selected && <UserDetail user={selected} onClose={() => setSelected(null)} />}

      <Card><CardBody>
        <span className="text-xs text-[var(--text-muted)]">Select a user to manage sessions, subscription, payments & security.</span>
      </CardBody></Card>
    </div>
  );
}

function AddUserForm({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("read_only");
  const [err, setErr] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: () => userCenterApi.create({ email, name, role }),
    onSuccess: () => { onDone(); onClose(); },
    onError: (e: Error) => setErr(e.message),
  });
  return (
    <Card className="border-accent-500/30">
      <CardHeader title="Add a user" />
      <CardBody className="grid gap-2 sm:grid-cols-3">
        <TextInput value={email} onChange={setEmail} placeholder="email@company.com" />
        <TextInput value={name} onChange={setName} placeholder="Full name" />
        <Select value={role} onChange={setRole}
                options={Object.entries(ROLE_LABELS).map(([k, l]) => ({ value: k, label: l }))} />
        <div className="sm:col-span-3 flex gap-2">
          {err && <span className="text-xs text-loss">{err}</span>}
          <div className="flex-1" />
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!email || !name || mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Adding…" : "Add user"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

function UserDetail({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const client = useQueryClient();
  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "users"] });
  const [subscription, setSubscription] = useState<UserSubscription | null>(null);
  const [sessions, setSessions] = useState<UserSession[]>([]);
  const [invoices, setInvoices] = useState<UserInvoice[]>([]);
  const [security, setSecurity] = useState<Record<string, unknown> | null>(null);
  const [tab, setTab] = useState("overview");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const loadSubscription = () => userCenterApi.subscription(user.id).then(setSubscription).catch(() => setSubscription(null));
  const loadSessions = () => userCenterApi.sessions(user.id).then(setSessions).catch(() => setSessions([]));
  const loadInvoices = () => userCenterApi.invoices(user.id).then(setInvoices).catch(() => setInvoices([]));
  const loadSecurity = () => userCenterApi.security(user.id).then(setSecurity).catch(() => setSecurity(null));
  const remove = useMutation({
    mutationFn: () => userCenterApi.delete(user.id),
    onSuccess: () => { invalidate(); onClose(); },
  });

  const changeSub = useMutation({
    mutationFn: (tier: string) => userCenterApi.changeSubscription(user.id, tier),
    onSuccess: loadSubscription,
  });
  const renew = useMutation({ mutationFn: () => userCenterApi.renewSubscription(user.id), onSuccess: loadSubscription });
  const logoutAll = useMutation({ mutationFn: () => userCenterApi.logoutAll(user.id), onSuccess: loadSessions });
  const issueInvoice = useMutation({
    mutationFn: (amt: number) => userCenterApi.issueInvoice(user.id, "pro", amt),
    onSuccess: loadInvoices,
  });

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/50 p-4">
      <div className="mt-4 w-full max-w-3xl rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-semibold text-[var(--text)]">{user.name} <span className="num text-accent-500">({ROLE_LABELS[user.role]})</span></div>
            <div className="text-xs text-[var(--text-muted)]">{user.email}</div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="danger" onClick={() => setConfirmDelete(true)}>Delete</Button>
            <button onClick={onClose} className="text-[var(--text-muted)]">✕</button>
          </div>
        </div>
        <div className="mt-3 flex gap-1 overflow-x-auto pb-1">
          {[["overview", "Overview"], ["sub", "Subscription"], ["sessions", "Sessions"], ["billing", "Billing"], ["security", "Security"]].map(([k, l]) => (
            <button key={k} onClick={() => { setTab(k); if (k === "sub") loadSubscription(); if (k === "sessions") loadSessions(); if (k === "billing") loadInvoices(); if (k === "security") loadSecurity(); }}
                    className={tab === k ? "shrink-0 rounded bg-accent-500/10 px-3 py-1.5 text-xs font-medium text-accent-500" : "shrink-0 rounded border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--text)]"}>
              {l}
            </button>
          ))}
        </div>

        {tab === "overview" && (
          <div className="mt-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MiniStat label="Status" value={user.status} />
            <MiniStat label="2FA" value={user.mfa_method !== "none" ? "Enabled" : "Off"} />
            <MiniStat label="Active sessions" value={String(user.active_sessions ?? 0)} />
            <MiniStat label="Failed logins" value={String(user.failed_login_count ?? 0)} />
          </div>
        )}

        {tab === "sub" && (
          <div className="mt-3 space-y-3">
            <div className="flex flex-wrap gap-2">
              {["free", "basic", "professional", "enterprise"].map((tier) => (
                <Button key={tier} variant={subscription?.plan_tier === tier ? "primary" : "ghost"}
                        onClick={() => changeSub.mutate(tier)} className="capitalize">{tier}</Button>
              ))}
              <Button variant="ghost" onClick={() => renew.mutate()}><KeyRound className="h-3.5 w-3.5" /> Renew</Button>
            </div>
            {subscription && (
              <div className="grid grid-cols-2 gap-3 text-xs">
                <MiniStat label="Plan" value={String(subscription.plan_tier)} />
                <MiniStat label="Status" value={String(subscription.status)} />
                <MiniStat label="Period" value={`${String(subscription.period_start)} → ${String(subscription.period_end)}`} />
                <MiniStat label="Billing" value={String(subscription.billing_period)} />
              </div>
            )}
          </div>
        )}

        {tab === "sessions" && (
          <div className="mt-3">
            <div className="mb-2 flex justify-end"><Button variant="danger" onClick={() => logoutAll.mutate()}>Logout all</Button></div>
            <div className="space-y-2">
              {(sessions ?? []).map((s) => (
                <div key={String(s.session_id)} className="flex items-center justify-between rounded border border-[var(--border)] px-3 py-2 text-xs">
                  <div>
                    <div className="text-[var(--text)]">{s.ip_address ?? "—"}</div>
                    <div className="text-[var(--text-muted)]">{String(s.user_agent ?? "").slice(0, 60)}</div>
                  </div>
                  <div className="text-[var(--text-muted)]">issued {formatWhen(String(s.issued_at))}</div>
                </div>
              ))}
              {sessions.length === 0 && <p className="text-xs text-[var(--text-muted)]">No active sessions.</p>}
            </div>
          </div>
        )}

        {tab === "billing" && (
          <div className="mt-3 space-y-2">
            <div className="flex gap-2">
              <Button variant="primary" onClick={() => issueInvoice.mutate(99900)}>Issue ₹999 invoice</Button>
            </div>
            <table className="w-full text-xs">
              <thead><tr><th className="text-left text-[var(--text-muted)]">#</th><th className="text-right text-[var(--text-muted)]">Amount</th><th className="text-left text-[var(--text-muted)]">Status</th></tr></thead>
              <tbody>{(invoices ?? []).map((inv) => (
                <tr key={String(inv.id)} className="border-t border-[var(--border)]">
                  <td className="py-2">{inv.number}</td>
                  <td className="num py-2 text-right">₹{(Number(inv.total_paise) / 100).toFixed(2)}</td>
                  <td className="py-2"><StatusPill status={String(inv.status)} /></td>
                </tr>
              ))}
              {invoices.length === 0 && <tr><td colSpan={3} className="py-4 text-center text-[var(--text-muted)]">No invoices.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "security" && (
          <div className="mt-3 grid grid-cols-2 gap-3 text-xs lg:grid-cols-3">
            {security ? Object.entries(security).map(([k, v]) => (
              <MiniStat key={k} label={k.replace(/_/g, " ")} value={String(v)} />
            )) : <p className="text-xs text-[var(--text-muted)]">Loading…</p>}
          </div>
        )}
      </div>

      {confirmDelete && (
        <div className="fixed inset-0 z-[95] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold text-[var(--text)]">Delete {user.name}?</div>
            <p className="mt-3 text-xs text-[var(--text-muted)]">This permanently removes the account and all its sessions.</p>
            <div className="mt-5 flex justify-end gap-2">
              <Button onClick={() => setConfirmDelete(false)}>Cancel</Button>
              <Button variant="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>Delete</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-[var(--border)] px-3 py-2">
      <div className="text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">{label}</div>
      <div className="mt-0.5 truncate text-sm font-medium text-[var(--text)]">{value}</div>
    </div>
  );
}

/* ==================================================================== */
function SubscriptionsTab() {
  const [page, setPage] = useState(1);
  const { data } = useQuery({
    queryKey: ["admin", "users", page], queryFn: () => userCenterApi.list({ page, page_size: 25 }),
  });
  return (
    <Card>
      <CardHeader title="Subscriptions by user" subtitle="Select a user's detail tab for upgrade / downgrade / renew / extend." />
      <CardBody className="p-0">
        <table className="w-full text-xs">
          <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">User</th><th className="px-3 py-2 text-left text-[var(--text-muted)]">Role</th><th className="px-3 py-2 text-left text-[var(--text-muted)]">Status</th></tr></thead>
          <tbody>{(data?.items ?? []).map((u) => (
            <tr key={u.id} className="border-t border-[var(--border)]">
              <td className="px-3 py-2 font-medium text-[var(--text)]">{u.name}</td>
              <td className="px-3 py-2">{ROLE_LABELS[u.role]}</td>
              <td className="px-3 py-2"><StatusPill status={u.status} /></td>
            </tr>))}
          </tbody>
        </table>
        <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
      </CardBody>
    </Card>
  );
}

function PaymentsTab() { return <Placeholder label="Payments & invoices" sub="Open a user detail → Billing to issue, pay or refund invoices." />; }
function SessionsTab() { return <Placeholder label="Sessions" sub="Open a user detail → Sessions to view devices and force logout." />; }
function SecurityTab() { return <Placeholder label="Security" sub="Open a user detail → Security for 2FA status, reset, verification and login history." />; }

function NotificationsTab() {
  const client = useQueryClient();
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const announce = useMutation({
    mutationFn: () => userCenterApi.announce(subject, body),
    onSuccess: () => { setSubject(""); setBody(""); client.invalidateQueries({ queryKey: ["admin"] }); },
  });
  return (
    <Card>
      <CardHeader title="Announcement" subtitle="Email / push announcement to all active users." />
      <CardBody className="space-y-3">
        <TextInput value={subject} onChange={setSubject} placeholder="Subject" />
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={3} placeholder="Body…"
                  className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
        <Button variant="primary" disabled={!subject || announce.isPending} onClick={() => announce.mutate()}>
          {announce.isPending ? "Sending…" : "Send announcement"}
        </Button>
      </CardBody>
    </Card>
  );
}

function PermissionsTab() {
  const { data } = useQuery({ queryKey: ["admin", "users", "roles"], queryFn: userCenterApi.roles });
  return (
    <Card>
      <CardHeader title="Roles & permissions" />
      <CardBody className="p-0">
        <table className="w-full text-xs">
          <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Role</th><th className="px-3 py-2 text-left text-[var(--text-muted)]">Permissions</th></tr></thead>
          <tbody>{(data?.roles ?? []).map((r: RoleInfo) => (
            <tr key={r.key} className="border-t border-[var(--border)]">
              <td className="px-3 py-2 font-medium text-[var(--text)]">{r.label}</td>
              <td className="px-3 py-2">
                <div className="flex flex-wrap gap-1">
                  {r.permissions.map((p) => <span key={p} className="rounded bg-[var(--bg-subtle)] px-1.5 py-0.5 text-[0.625rem] text-[var(--text-muted)]">{p}</span>)}
                </div>
              </td>
            </tr>))}
          </tbody>
        </table>
      </CardBody>
    </Card>
  );
}

function AnalyticsTab() {
  const { data } = useQuery({ queryKey: ["admin", "users", "analytics"], queryFn: () => userCenterApi.analytics(30) });
  const cards = data ? [
    { label: "Total users", value: data.total_users },
    { label: "Active users", value: data.active_users },
    { label: "New (30d)", value: data.new_users },
    { label: "Premium", value: data.premium_users },
    { label: "Free", value: data.free_users },
    { label: "Revenue (₹)", value: data.revenue_inr },
    { label: "Tenants", value: data.tenants },
    { label: "Retention", value: `${data.retention_pct}%` },
  ] : [];
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {cards.map((c) => (
        <Card key={c.label}><CardBody>
          <div className="text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">{c.label}</div>
          <div className="mt-1 text-xl font-semibold text-[var(--text)]">{c.value}</div>
        </CardBody></Card>
      ))}
    </div>
  );
}

function Placeholder({ label, sub }: { label: string; sub: string }) {
  return <Card><CardBody className="text-xs text-[var(--text-muted)]"><b className="text-[var(--text)]">{label}</b> — {sub}</CardBody></Card>;
}
