"use client";

/**
 * The tenant administration console.
 *
 * Six tabs, one organisation: whoever is signed in administers their own
 * workspace and nothing else. There is no tenant selector, because there is
 * no tenant to select — the backend filters everything to the caller's
 * organisation and would refuse a request for another's.
 *
 * The page holds no business logic. Quota utilisation, entitlement decisions
 * and audit severity all arrive computed; this file decides where they sit on
 * the screen.
 */

import { adminApi } from "@/lib/api";
import { ApiError } from "@/lib/api";
import type {
  ApiKey, AuditRow, Entitlements, IssuedApiKey, PlatformUser, TenantOverview,
} from "@/lib/types";
import {
  Button, DataTable, KeyValue, Pager, QuotaBar, Select, Sparkline, StatusPill,
  Tabs, TextInput, formatBytes, formatInr, formatWhen,
} from "@/components/admin/primitives";
import { AppShell } from "@/components/layout/app-shell";
import { Card, CardBody, CardHeader, EmptyState, Skeleton, Stat } from "@/components/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, Building2, Check, Copy, CreditCard, Database, Key,
  RefreshCw, ScrollText, Send, Shield, Trash2, TrendingUp, Users,
} from "lucide-react";
import { useState } from "react";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "members", label: "Members" },
  { key: "billing", label: "Subscription" },
  { key: "usage", label: "Usage" },
  { key: "keys", label: "API Keys" },
  { key: "audit", label: "Audit Log" },
] as const;

export default function AdminPage() {
  const [tab, setTab] = useState<string>("overview");

  return (
    <AppShell>
    <div className="mx-auto max-w-[1400px] space-y-4 p-4 lg:p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-[var(--text)]">Administration</h1>
          <p className="mt-0.5 text-xs text-[var(--text-muted)]">
            Members, subscription, usage and the audit trail for your organisation.
          </p>
        </div>
      </header>

      <Tabs tabs={TABS.map((t) => ({ key: t.key, label: t.label }))} active={tab} onChange={setTab} />

      {tab === "overview" && <OverviewTab />}
      {tab === "members" && <MembersTab />}
      {tab === "billing" && <BillingTab />}
      {tab === "usage" && <UsageTab />}
      {tab === "keys" && <ApiKeysTab />}
      {tab === "audit" && <AuditTab />}
    </div>
    </AppShell>
  );
}

/* ====================================================================== */
function OverviewTab() {
  const { data, isLoading, error } = useQuery<TenantOverview>({
    queryKey: ["admin", "overview"], queryFn: adminApi.overview,
  });

  if (isLoading) return <LoadingGrid />;
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;

  const daily = data.audit_7d?.daily?.map((d) => d.count) ?? [];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Card><CardBody>
          <Stat label="Plan" value={data.plan} hint={<StatusPill status={data.status} />} mono={false} />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Members" value={data.members_active}
                hint={`${data.members} total`} />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Storage" value={formatBytes(data.storage.total ?? 0)}
                hint={`${data.documents} documents · ${data.reports} reports`} mono={false} />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Renews in" value={`${data.days_remaining}d`}
                hint={new Date(data.period_end).toLocaleDateString("en-IN")} />
        </CardBody></Card>
      </div>

      {data.nearing_limit.length > 0 && (
        <Card className="border-warn/30">
          <CardHeader
            title="Approaching your plan limits"
            subtitle="These will start refusing requests once exhausted."
            action={<AlertTriangle className="h-4 w-4 text-warn" />}
          />
          <CardBody className="space-y-3">
            {data.nearing_limit.map((q) => (
              <QuotaBar key={q.quota} label={q.label} used={q.used}
                        allowance={q.allowance} unlimited={q.unlimited}
                        utilisation={q.utilisation} unit={q.unit} />
            ))}
          </CardBody>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Quota consumption" subtitle="Current billing period" />
          <CardBody className="grid gap-3 sm:grid-cols-2">
            {data.quotas.map((q) => (
              <QuotaBar key={q.quota} label={q.label} used={q.used}
                        allowance={q.allowance} unlimited={q.unlimited}
                        utilisation={q.utilisation} unit={q.unit} />
            ))}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Activity" subtitle="Audited events, last 7 days" />
          <CardBody>
            <div className="flex items-baseline gap-3">
              <span className="num text-2xl font-semibold text-[var(--text)]">
                {data.audit_7d?.total ?? 0}
              </span>
              {(data.audit_7d?.failures ?? 0) > 0 && (
                <StatusPill status="failure" />
              )}
            </div>
            {daily.length > 1 && <Sparkline points={daily} className="mt-3" />}
            <div className="mt-3 space-y-0">
              {Object.entries(data.audit_7d?.by_category ?? {})
                .sort((a, b) => b[1] - a[1]).slice(0, 5)
                .map(([category, count]) => (
                  <KeyValue key={category} label={category} value={count} />
                ))}
            </div>
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Organisation" action={<Building2 className="h-4 w-4 text-[var(--text-muted)]" />} />
          <CardBody>
            <KeyValue label="Name" value={data.tenant.name} />
            <KeyValue label="Identifier" value={<code className="num">{data.tenant.slug}</code>} />
            <KeyValue label="Industry" value={data.tenant.industry ?? "—"} />
            <KeyValue label="Country" value={data.tenant.country} />
            <KeyValue label="Created" value={formatWhen(data.tenant.created_at)} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Storage breakdown" action={<Database className="h-4 w-4 text-[var(--text-muted)]" />} />
          <CardBody>
            <KeyValue label="Documents" value={formatBytes(data.storage.documents ?? 0)} />
            <KeyValue label="Report artefacts" value={formatBytes(data.storage.reports ?? 0)} />
            <KeyValue label="Total" value={formatBytes(data.storage.total ?? 0)} />
            <KeyValue label="Active API keys" value={data.api_keys.active ?? 0} />
            <KeyValue label="API calls" value={(data.api_keys.calls ?? 0).toLocaleString("en-IN")} />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

/* ====================================================================== */
function MembersTab() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const [inviting, setInviting] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "members", page, search, role],
    queryFn: () => adminApi.members({ page, page_size: 25, search, role }),
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "members"] });

  const setRoleMutation = useMutation({
    mutationFn: ({ id, value }: { id: string; value: string }) =>
      adminApi.setMemberRole(id, value),
    onSuccess: invalidate,
  });

  const setStatusMutation = useMutation({
    mutationFn: ({ id, value }: { id: string; value: string }) =>
      adminApi.setMemberStatus(id, value),
    onSuccess: invalidate,
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => adminApi.removeMember(id),
    onSuccess: invalidate,
  });

  const mutationError =
    setRoleMutation.error ?? setStatusMutation.error ?? removeMutation.error;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }}
                   placeholder="Search name or email…" className="max-w-xs" />
        <Select value={role} onChange={(v) => { setRole(v); setPage(1); }}
                options={[
                  { value: "", label: "All roles" },
                  { value: "admin", label: "Admin" },
                  { value: "analyst", label: "Analyst" },
                  { value: "researcher", label: "Researcher" },
                  { value: "subscriber", label: "Subscriber" },
                  { value: "read_only", label: "Read Only" },
                ]} />
        <div className="flex-1" />
        <Button variant="primary" onClick={() => setInviting(true)}>
          <Send className="h-3.5 w-3.5" /> Invite member
        </Button>
      </div>

      {mutationError && <ErrorNotice error={mutationError} />}
      {inviting && <InviteForm onClose={() => setInviting(false)} onDone={invalidate} />}

      <Card>
        {isLoading ? <Skeleton className="h-64" /> : error ? <ErrorNotice error={error} /> : (
          <>
            <DataTable<PlatformUser>
              rowKey={(row) => row.id}
              rows={data?.items ?? []}
              empty="No members match this filter."
              columns={[
                {
                  key: "name", header: "Member",
                  render: (row) => (
                    <div className="min-w-0">
                      <div className="truncate font-medium text-[var(--text)]">{row.name}</div>
                      <div className="truncate text-xs text-[var(--text-muted)]">{row.email}</div>
                    </div>
                  ),
                },
                {
                  key: "role", header: "Role", width: "160px",
                  render: (row) => (
                    <Select
                      value={row.role}
                      onChange={(value) => setRoleMutation.mutate({ id: row.id, value })}
                      options={[
                        { value: "admin", label: "Admin" },
                        { value: "analyst", label: "Analyst" },
                        { value: "researcher", label: "Researcher" },
                        { value: "subscriber", label: "Subscriber" },
                        { value: "read_only", label: "Read Only" },
                        { value: "guest", label: "Guest" },
                      ]}
                      className="w-full text-xs"
                    />
                  ),
                },
                {
                  key: "status", header: "Status", width: "110px",
                  render: (row) => <StatusPill status={row.status} />,
                },
                {
                  key: "seen", header: "Last seen", width: "110px", align: "right",
                  render: (row) => (
                    <span className="text-xs text-[var(--text-muted)]">
                      {formatWhen(row.last_seen_at)}
                    </span>
                  ),
                },
                {
                  key: "actions", header: "", width: "150px", align: "right",
                  render: (row) => (
                    <div className="flex justify-end gap-1">
                      {row.status === "active" ? (
                        <Button variant="ghost"
                                onClick={() => setStatusMutation.mutate({ id: row.id, value: "suspended" })}>
                          Suspend
                        </Button>
                      ) : (
                        <Button variant="ghost"
                                onClick={() => setStatusMutation.mutate({ id: row.id, value: "active" })}>
                          Reactivate
                        </Button>
                      )}
                      <Button variant="danger" onClick={() => removeMutation.mutate(row.id)}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  ),
                },
              ]}
            />
            <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
          </>
        )}
      </Card>

      <p className="text-xs text-[var(--text-muted)]">
        Removing a member deactivates their account and revokes every session
        and API key. Their research — reports, portfolios, transactions —
        is retained, because it is referenced across four modules and may be
        needed years later.
      </p>
    </div>
  );
}

function InviteForm({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("read_only");

  const mutation = useMutation({
    mutationFn: () => adminApi.invite({ email, name, role }),
    onSuccess: () => { onDone(); onClose(); },
  });

  return (
    <Card className="border-accent-500/30">
      <CardHeader title="Invite a member"
                  subtitle="They receive a single-use link and choose their own password." />
      <CardBody className="space-y-3">
        <div className="grid gap-2 sm:grid-cols-3">
          <TextInput value={email} onChange={setEmail} placeholder="email@company.com" type="email" />
          <TextInput value={name} onChange={setName} placeholder="Full name" />
          <Select value={role} onChange={setRole}
                  options={[
                    { value: "read_only", label: "Read Only" },
                    { value: "subscriber", label: "Subscriber" },
                    { value: "researcher", label: "Researcher" },
                    { value: "analyst", label: "Analyst" },
                    { value: "admin", label: "Admin" },
                  ]} />
        </div>
        {mutation.error && <ErrorNotice error={mutation.error} />}
        <div className="flex gap-2">
          <Button variant="primary" disabled={!email || !name || mutation.isPending}
                  onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Sending…" : "Send invitation"}
          </Button>
          <Button onClick={onClose}>Cancel</Button>
        </div>
      </CardBody>
    </Card>
  );
}

/* ====================================================================== */
function BillingTab() {
  const client = useQueryClient();
  const { data, isLoading, error } = useQuery<Entitlements>({
    queryKey: ["admin", "entitlements"], queryFn: adminApi.entitlements,
  });

  const changePlan = useMutation({
    mutationFn: (tier: string) => adminApi.changePlan(tier),
    onSuccess: () => client.invalidateQueries({ queryKey: ["admin"] }),
  });

  if (isLoading) return <LoadingGrid />;
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;

  return (
    <div className="space-y-4">
      {data.warnings.length > 0 && (
        <Card className="border-warn/30">
          <CardBody className="space-y-1">
            {data.warnings.map((warning) => (
              <p key={warning} className="flex items-start gap-2 text-xs text-warn">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{warning}
              </p>
            ))}
          </CardBody>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="Current plan" action={<CreditCard className="h-4 w-4 text-[var(--text-muted)]" />} />
          <CardBody>
            <Stat label={data.plan_name} value={<StatusPill status={data.status} />} mono={false} />
            <div className="mt-3">
              <KeyValue label="Period" value={
                `${new Date(data.period_start).toLocaleDateString("en-IN")} – ${new Date(data.period_end).toLocaleDateString("en-IN")}`
              } />
              <KeyValue label="Days remaining" value={data.days_remaining} />
              {data.trial_ends_at && (
                <KeyValue label="Trial ends" value={new Date(data.trial_ends_at).toLocaleDateString("en-IN")} />
              )}
              <KeyValue label="Auto-renew" value={data.cancel_at_period_end ? "No" : "Yes"} />
            </div>
          </CardBody>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader title="Change plan" subtitle="Upgrades take effect immediately." />
          <CardBody>
            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
              {["free", "basic", "professional", "enterprise"].map((tier) => (
                <button
                  key={tier}
                  type="button"
                  disabled={tier === data.plan_tier || changePlan.isPending}
                  onClick={() => changePlan.mutate(tier)}
                  className={
                    tier === data.plan_tier
                      ? "rounded border border-accent-500 bg-accent-500/10 px-3 py-3 text-xs font-medium capitalize text-accent-500"
                      : "rounded border border-[var(--border)] px-3 py-3 text-xs font-medium capitalize text-[var(--text)] transition-colors hover:bg-[var(--bg-subtle)]"
                  }
                >
                  {tier}
                  {tier === data.plan_tier && <div className="mt-1 text-[0.625rem]">Current</div>}
                </button>
              ))}
            </div>
            {changePlan.error && <div className="mt-3"><ErrorNotice error={changePlan.error} /></div>}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Features" subtitle="What this plan includes" />
          <CardBody className="grid gap-1 sm:grid-cols-2">
            {data.all_features.map((feature) => (
              <div key={feature.key} className="flex items-center gap-1.5 text-xs">
                {feature.included
                  ? <Check className="h-3.5 w-3.5 shrink-0 text-gain" />
                  : <span className="h-3.5 w-3.5 shrink-0 text-center text-[var(--text-muted)]">–</span>}
                <span className={feature.included ? "text-[var(--text)]" : "text-[var(--text-muted)] line-through"}>
                  {feature.label}
                </span>
              </div>
            ))}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Limits" subtitle="Ceilings that do not reset each period" />
          <CardBody>
            {data.limits.map((limit) => (
              <KeyValue key={limit.limit} label={limit.label}
                        value={`${limit.used.toLocaleString("en-IN")} / ${limit.unlimited ? "∞" : limit.allowance.toLocaleString("en-IN")}`} />
            ))}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

/* ====================================================================== */
function UsageTab() {
  const [days, setDays] = useState(30);
  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "usage", days], queryFn: () => adminApi.usage(days),
  });

  if (isLoading) return <LoadingGrid />;
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Select value={String(days)} onChange={(v) => setDays(Number(v))}
                options={[
                  { value: "7", label: "Last 7 days" },
                  { value: "30", label: "Last 30 days" },
                  { value: "90", label: "Last 90 days" },
                ]} />
        <div className="flex-1" />
        <span className="text-xs text-[var(--text-muted)]">
          Provider cost this period: <span className="num">${data.cost_usd.toFixed(4)}</span>
        </span>
      </div>

      <Card>
        <CardHeader title="This period" subtitle={`${data.period_start} to ${data.period_end}`} />
        <CardBody className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {data.quotas.map((quota) => (
            <QuotaBar key={quota.quota} label={quota.label} used={quota.used}
                      allowance={quota.allowance} unlimited={quota.unlimited}
                      utilisation={quota.utilisation} unit={quota.unit} />
          ))}
        </CardBody>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {data.series.map((series) => (
          <Card key={series.quota}>
            <CardHeader title={series.label}
                        subtitle={`${series.total.toLocaleString("en-IN")} ${series.unit} over ${days} days`} />
            <CardBody>
              {series.points.length > 1
                ? <Sparkline points={series.points.map((p) => p.value)} height={48} className="h-12" />
                : <p className="text-xs text-[var(--text-muted)]">Not enough data to chart.</p>}
            </CardBody>
          </Card>
        ))}
      </div>

      {data.top_users.length > 0 && (
        <Card>
          <CardHeader title="Most active members" subtitle={`Metered units, last ${days} days`} />
          <DataTable
            rowKey={(row) => row.user_id}
            rows={data.top_users}
            columns={[
              { key: "name", header: "Member", render: (r) => r.name },
              { key: "units", header: "Units", align: "right",
                render: (r) => <span className="num">{r.units.toLocaleString("en-IN")}</span> },
              { key: "events", header: "Events", align: "right",
                render: (r) => <span className="num">{r.events.toLocaleString("en-IN")}</span> },
            ]}
          />
        </Card>
      )}
    </div>
  );
}

/* ====================================================================== */
function ApiKeysTab() {
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<IssuedApiKey | null>(null);
  const [copied, setCopied] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "api-keys"], queryFn: () => adminApi.apiKeys(true),
  });

  const revoke = useMutation({
    mutationFn: (id: number) => adminApi.revokeApiKey(id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["admin", "api-keys"] }),
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <p className="text-xs text-[var(--text-muted)]">
          Keys authenticate as <code>X-API-Key</code> or a bearer token, and
          never carry more privilege than the person who created them.
        </p>
        <div className="flex-1" />
        <Button variant="primary" onClick={() => setCreating(true)}>
          <Key className="h-3.5 w-3.5" /> Create key
        </Button>
      </div>

      {creating && (
        <CreateKeyForm
          onClose={() => setCreating(false)}
          onIssued={(key) => {
            setIssued(key); setCreating(false);
            client.invalidateQueries({ queryKey: ["admin", "api-keys"] });
          }}
        />
      )}

      {issued && (
        <Card className="border-warn/40">
          <CardHeader title="Copy this key now"
                      subtitle="It is stored only as a hash and cannot be shown again." />
          <CardBody className="space-y-2">
            <div className="flex items-center gap-2">
              <code className="num flex-1 overflow-x-auto rounded border border-[var(--border)] bg-[var(--bg-subtle)] px-2 py-1.5 text-xs">
                {issued.plaintext}
              </code>
              <Button onClick={() => {
                navigator.clipboard?.writeText(issued.plaintext);
                setCopied(true);
              }}>
                {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                {copied ? "Copied" : "Copy"}
              </Button>
              <Button variant="ghost" onClick={() => { setIssued(null); setCopied(false); }}>
                Dismiss
              </Button>
            </div>
          </CardBody>
        </Card>
      )}

      <Card>
        {isLoading ? <Skeleton className="h-48" /> : error ? <ErrorNotice error={error} /> : (
          <DataTable<ApiKey>
            rowKey={(row) => row.id}
            rows={data ?? []}
            empty="No API keys yet."
            columns={[
              { key: "name", header: "Name", render: (r) => r.name },
              { key: "key", header: "Key", render: (r) => <code className="num text-xs">{r.masked}</code> },
              { key: "role", header: "Role", width: "110px",
                render: (r) => <StatusPill status={r.role} /> },
              { key: "used", header: "Last used", width: "110px", align: "right",
                render: (r) => <span className="text-xs text-[var(--text-muted)]">{formatWhen(r.last_used_at)}</span> },
              { key: "calls", header: "Calls", width: "90px", align: "right",
                render: (r) => <span className="num text-xs">{r.call_count.toLocaleString("en-IN")}</span> },
              {
                key: "actions", header: "", width: "100px", align: "right",
                render: (r) => r.revoked_at
                  ? <StatusPill status="revoked" />
                  : <Button variant="danger" onClick={() => revoke.mutate(r.id)}>Revoke</Button>,
              },
            ]}
          />
        )}
      </Card>
    </div>
  );
}

function CreateKeyForm({
  onClose, onIssued,
}: { onClose: () => void; onIssued: (key: IssuedApiKey) => void }) {
  const [name, setName] = useState("");
  const [role, setRole] = useState("read_only");
  const [days, setDays] = useState("365");

  const mutation = useMutation({
    mutationFn: () => adminApi.createApiKey({ name, role, expires_in_days: Number(days) }),
    onSuccess: onIssued,
  });

  return (
    <Card className="border-accent-500/30">
      <CardHeader title="Create an API key" />
      <CardBody className="space-y-3">
        <div className="grid gap-2 sm:grid-cols-3">
          <TextInput value={name} onChange={setName} placeholder="What is it for?" />
          <Select value={role} onChange={setRole}
                  options={[
                    { value: "read_only", label: "Read Only" },
                    { value: "subscriber", label: "Subscriber" },
                    { value: "researcher", label: "Researcher" },
                    { value: "analyst", label: "Analyst" },
                  ]} />
          <Select value={days} onChange={setDays}
                  options={[
                    { value: "30", label: "Expires in 30 days" },
                    { value: "90", label: "Expires in 90 days" },
                    { value: "365", label: "Expires in 1 year" },
                    { value: "730", label: "Expires in 2 years" },
                  ]} />
        </div>
        {mutation.error && <ErrorNotice error={mutation.error} />}
        <div className="flex gap-2">
          <Button variant="primary" disabled={!name || mutation.isPending}
                  onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Creating…" : "Create key"}
          </Button>
          <Button onClick={onClose}>Cancel</Button>
        </div>
      </CardBody>
    </Card>
  );
}

/* ====================================================================== */
function AuditTab() {
  const [page, setPage] = useState(1);
  const [category, setCategory] = useState("");
  const [outcome, setOutcome] = useState("");
  const [search, setSearch] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "audit", page, category, outcome, search],
    queryFn: () => adminApi.audit({ page, page_size: 25, category, outcome, search, days: 90 }),
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }}
                   placeholder="Search summary, actor or action…" className="max-w-xs" />
        <Select value={category} onChange={(v) => { setCategory(v); setPage(1); }}
                options={[
                  { value: "", label: "All categories" },
                  { value: "auth", label: "Authentication" },
                  { value: "account", label: "Accounts" },
                  { value: "security", label: "Security" },
                  { value: "billing", label: "Billing" },
                  { value: "report", label: "Reports" },
                  { value: "document", label: "Documents" },
                  { value: "portfolio", label: "Portfolio" },
                  { value: "ai", label: "AI" },
                ]} />
        <Select value={outcome} onChange={(v) => { setOutcome(v); setPage(1); }}
                options={[
                  { value: "", label: "All outcomes" },
                  { value: "success", label: "Success" },
                  { value: "failure", label: "Failure" },
                  { value: "denied", label: "Denied" },
                ]} />
      </div>

      <Card>
        {isLoading ? <Skeleton className="h-96" /> : error ? <ErrorNotice error={error} /> : (
          <>
            <DataTable<AuditRow>
              rowKey={(row) => row.id}
              rows={data?.items ?? []}
              empty="No audited events match this filter."
              columns={[
                {
                  key: "when", header: "When", width: "110px",
                  render: (r) => (
                    <span className="text-xs text-[var(--text-muted)]" title={r.occurred_at}>
                      {formatWhen(r.occurred_at)}
                    </span>
                  ),
                },
                {
                  key: "action", header: "Action", width: "220px",
                  render: (r) => <code className="text-xs text-[var(--text)]">{r.action}</code>,
                },
                {
                  key: "actor", header: "Actor", width: "200px",
                  render: (r) => (
                    <span className="truncate text-xs text-[var(--text-muted)]">
                      {r.actor_email ?? "system"}
                    </span>
                  ),
                },
                { key: "summary", header: "Summary", render: (r) => (
                  <span className="text-xs text-[var(--text)]">{r.summary}</span>
                ) },
                {
                  key: "outcome", header: "Outcome", width: "100px", align: "right",
                  render: (r) => <StatusPill status={r.outcome} />,
                },
              ]}
            />
            <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
          </>
        )}
      </Card>

      <p className="text-xs text-[var(--text-muted)]">
        The trail is append-only. Credentials are redacted before an entry is
        written, so no API key, token or password appears here whatever the
        calling code passed.
      </p>
    </div>
  );
}

/* ====================================================================== */
function LoadingGrid() {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24" />)}
    </div>
  );
}

function ErrorNotice({ error }: { error: unknown }) {
  const status = error instanceof ApiError ? error.status : null;
  const message = error instanceof Error ? error.message : String(error);

  // 402 is a commercial refusal, not a failure. Saying "error" to someone who
  // simply needs a higher plan is both wrong and unhelpful.
  const commercial = status === 402;

  return (
    <div className={
      commercial
        ? "rounded border border-accent-500/30 bg-accent-500/5 px-3 py-2 text-xs text-accent-500"
        : "rounded border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss"
    }>
      <span className="font-medium">{commercial ? "Upgrade required" : "Something went wrong"}</span>
      {" — "}{message}
    </div>
  );
}
