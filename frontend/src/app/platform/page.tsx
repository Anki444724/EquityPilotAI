"use client";

/**
 * The platform operator console.
 *
 * Everything here is cross-tenant and reachable only by a Super Admin. The
 * backend returns 404 rather than 403 to anyone else, so a customer probing
 * these URLs learns nothing — including that they exist.
 *
 * The tabs answer the questions an operator actually has, in the order they
 * tend to arise: is the estate healthy, who is on it, what is it earning,
 * what is broken, and is there a restorable backup.
 */

import { ApiError, platformApi } from "@/lib/api";
import type {
  BackgroundJob, BackupRecord, PlatformOverview, PlatformTenant, PlatformUser,
  Plan, RouteMetric, Schedule, TrackedError,
} from "@/lib/types";
import {
  Button, DataTable, KeyValue, Pager, Select, Sparkline, StatusPill, Tabs,
  TextInput, formatBytes, formatInr, formatWhen,
} from "@/components/admin/primitives";
import { AppShell } from "@/components/layout/app-shell";
import { Card, CardBody, CardHeader, Skeleton, Stat } from "@/components/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, AlertTriangle, Bug, Building2, Database, HardDrive, Layers,
  RefreshCw, ShieldCheck, Timer, Users,
} from "lucide-react";
import { useState } from "react";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "tenants", label: "Organisations" },
  { key: "users", label: "Users" },
  { key: "plans", label: "Plans" },
  { key: "jobs", label: "Jobs & Queue" },
  { key: "health", label: "Health & Errors" },
  { key: "backups", label: "Backups" },
] as const;

export default function PlatformPage() {
  const [tab, setTab] = useState<string>("overview");

  return (
    <AppShell>
    <div className="mx-auto max-w-[1400px] space-y-4 p-4 lg:p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold text-[var(--text)]">
            <ShieldCheck className="h-4.5 w-4.5 text-accent-500" />
            Platform operations
          </h1>
          <p className="mt-0.5 text-xs text-[var(--text-muted)]">
            Every organisation on this deployment. Super Admin only.
          </p>
        </div>
      </header>

      <Tabs tabs={TABS.map((t) => ({ key: t.key, label: t.label }))} active={tab} onChange={setTab} />

      {tab === "overview" && <OverviewTab />}
      {tab === "tenants" && <TenantsTab />}
      {tab === "users" && <UsersTab />}
      {tab === "plans" && <PlansTab />}
      {tab === "jobs" && <JobsTab />}
      {tab === "health" && <HealthTab />}
      {tab === "backups" && <BackupsTab />}
    </div>
    </AppShell>
  );
}

/* ====================================================================== */
function OverviewTab() {
  const { data, isLoading, error } = useQuery<PlatformOverview>({
    queryKey: ["platform", "overview"], queryFn: platformApi.overview,
    refetchInterval: 30_000,
  });

  const { data: timeseries } = useQuery({
    queryKey: ["platform", "timeseries"],
    queryFn: () => platformApi.metricsTimeseries(180),
  });

  if (isLoading) return <LoadingGrid />;
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Card><CardBody>
          <Stat label="Organisations" value={data.tenants}
                hint={`${data.tenants_active} active · ${data.tenants_trial} trial`} />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Monthly recurring" value={formatInr(data.mrr_inr)}
                hint={`${formatInr(data.arr_inr)} annualised`} mono={false} />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Users" value={data.users}
                hint={`${data.users_active} active`} />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Health"
                value={<StatusPill status={data.health} />}
                hint={`${(data.error_rate * 100).toFixed(2)}% error rate`} mono={false} />
        </CardBody></Card>
      </div>

      {data.tenants_past_due > 0 && (
        <Card className="border-warn/30">
          <CardBody>
            <p className="flex items-center gap-2 text-xs text-warn">
              <AlertTriangle className="h-3.5 w-3.5" />
              <span className="num">{data.tenants_past_due}</span> organisation(s)
              are past due and running read-only.
            </p>
          </CardBody>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Request volume" subtitle="Last three hours"
                      action={<Activity className="h-4 w-4 text-[var(--text-muted)]" />} />
          <CardBody>
            {timeseries && timeseries.length > 1 ? (
              <>
                <Sparkline points={timeseries.map((p) => p.requests)} height={64} className="h-16" />
                <div className="mt-3 grid grid-cols-3 gap-3">
                  <Stat label="Requests / 24h" value={data.requests_24h.toLocaleString("en-IN")} />
                  <Stat label="AI calls / 30d" value={data.ai_calls_30d.toLocaleString("en-IN")} />
                  <Stat label="Storage" value={formatBytes(data.storage_bytes)} mono={false} />
                </div>
              </>
            ) : (
              <p className="text-xs text-[var(--text-muted)]">
                Not enough traffic yet to plot. Metrics accumulate in one-minute buckets.
              </p>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Queue" subtitle="Background work"
                      action={<Layers className="h-4 w-4 text-[var(--text-muted)]" />} />
          <CardBody>
            <div className="mb-3 flex items-center gap-2">
              <StatusPill status={data.queue.healthy ? "ok" : "degraded"} />
              {data.queue.dead_letter > 0 && (
                <span className="text-xs text-loss">
                  {data.queue.dead_letter} dead-lettered
                </span>
              )}
            </div>
            <KeyValue label="Queued" value={data.queue.queued} />
            <KeyValue label="Running" value={data.queue.running} />
            <KeyValue label="Retrying" value={data.queue.failed} />
            <KeyValue label="Dead letter" value={data.queue.dead_letter} />
            <KeyValue label="Succeeded (24h)" value={data.queue.succeeded_24h} />
            <KeyValue label="p95 duration"
                      value={`${data.queue.p95_duration_ms.toFixed(0)} ms`} />
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Plan distribution" />
          <CardBody>
            {Object.entries(data.tier_distribution).map(([tier, count]) => (
              <KeyValue key={tier} label={tier} value={count} />
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Content" />
          <CardBody>
            <KeyValue label="Documents" value={data.documents.toLocaleString("en-IN")} />
            <KeyValue label="Reports" value={data.reports.toLocaleString("en-IN")} />
            <KeyValue label="Portfolios" value={data.portfolios.toLocaleString("en-IN")} />
            <KeyValue label="Storage" value={formatBytes(data.storage_bytes)} />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

/* ====================================================================== */
function TenantsTab() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["platform", "tenants", page, search, status],
    queryFn: () => platformApi.tenants({ page, page_size: 25, search, status }),
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["platform"] });

  const suspend = useMutation({
    mutationFn: (id: number) => platformApi.suspendTenant(id, "Suspended from the operator console"),
    onSuccess: invalidate,
  });
  const reactivate = useMutation({
    mutationFn: (id: number) => platformApi.reactivateTenant(id),
    onSuccess: invalidate,
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }}
                   placeholder="Search name or slug…" className="max-w-xs" />
        <Select value={status} onChange={(v) => { setStatus(v); setPage(1); }}
                options={[
                  { value: "", label: "All statuses" },
                  { value: "active", label: "Active" },
                  { value: "trial", label: "Trial" },
                  { value: "past_due", label: "Past due" },
                  { value: "suspended", label: "Suspended" },
                ]} />
      </div>

      {(suspend.error || reactivate.error) && (
        <ErrorNotice error={suspend.error ?? reactivate.error} />
      )}

      <Card>
        {isLoading ? <Skeleton className="h-72" /> : error ? <ErrorNotice error={error} /> : (
          <>
            <DataTable<PlatformTenant>
              rowKey={(row) => row.id}
              rows={data?.items ?? []}
              empty="No organisations match this filter."
              columns={[
                {
                  key: "name", header: "Organisation",
                  render: (r) => (
                    <div className="min-w-0">
                      <div className="truncate font-medium text-[var(--text)]">{r.name}</div>
                      <code className="truncate text-xs text-[var(--text-muted)]">{r.slug}</code>
                    </div>
                  ),
                },
                { key: "status", header: "Status", width: "110px",
                  render: (r) => <StatusPill status={r.status} /> },
                { key: "members", header: "Members", width: "90px", align: "right",
                  render: (r) => <span className="num text-xs">{r.member_count}</span> },
                { key: "storage", header: "Storage", width: "100px", align: "right",
                  render: (r) => <span className="text-xs">{formatBytes(r.storage_bytes)}</span> },
                { key: "created", header: "Created", width: "110px", align: "right",
                  render: (r) => <span className="text-xs text-[var(--text-muted)]">{formatWhen(r.created_at)}</span> },
                {
                  key: "actions", header: "", width: "120px", align: "right",
                  render: (r) => r.status === "suspended"
                    ? <Button onClick={() => reactivate.mutate(r.id)}>Reactivate</Button>
                    : <Button variant="danger" onClick={() => suspend.mutate(r.id)}>Suspend</Button>,
                },
              ]}
            />
            <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
          </>
        )}
      </Card>

      <p className="text-xs text-[var(--text-muted)]">
        Suspending an organisation revokes every session belonging to it
        immediately, rather than waiting up to fifteen minutes for access
        tokens to expire.
      </p>
    </div>
  );
}

/* ====================================================================== */
function UsersTab() {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["platform", "users", page, search, role],
    queryFn: () => platformApi.users({ page, page_size: 25, search, role }),
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }}
                   placeholder="Search name or email…" className="max-w-xs" />
        <Select value={role} onChange={(v) => { setRole(v); setPage(1); }}
                options={[
                  { value: "", label: "All roles" },
                  { value: "super_admin", label: "Super Admin" },
                  { value: "admin", label: "Admin" },
                  { value: "analyst", label: "Analyst" },
                  { value: "researcher", label: "Researcher" },
                  { value: "subscriber", label: "Subscriber" },
                  { value: "read_only", label: "Read Only" },
                ]} />
      </div>

      <Card>
        {isLoading ? <Skeleton className="h-72" /> : error ? <ErrorNotice error={error} /> : (
          <>
            <DataTable<PlatformUser>
              rowKey={(row) => row.id}
              rows={data?.items ?? []}
              empty="No users match this filter."
              columns={[
                {
                  key: "name", header: "User",
                  render: (r) => (
                    <div className="min-w-0">
                      <div className="truncate font-medium text-[var(--text)]">{r.name}</div>
                      <div className="truncate text-xs text-[var(--text-muted)]">{r.email}</div>
                    </div>
                  ),
                },
                { key: "role", header: "Role", width: "120px",
                  render: (r) => <StatusPill status={r.role} /> },
                { key: "status", header: "Status", width: "100px",
                  render: (r) => <StatusPill status={r.status} /> },
                { key: "tenant", header: "Org", width: "70px", align: "right",
                  render: (r) => <span className="num text-xs text-[var(--text-muted)]">{r.tenant_id ?? "—"}</span> },
                { key: "login", header: "Last login", width: "110px", align: "right",
                  render: (r) => <span className="text-xs text-[var(--text-muted)]">{formatWhen(r.last_login_at)}</span> },
              ]}
            />
            <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}

/* ====================================================================== */
function PlansTab() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["platform", "plans"], queryFn: () => platformApi.plans(false),
  });

  if (isLoading) return <LoadingGrid />;
  if (error) return <ErrorNotice error={error} />;

  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--text-muted)]">
        Plans are stored data, not code. Repricing takes effect immediately
        and survives a redeploy — the code catalogue seeds a fresh install and
        never overwrites an edit.
      </p>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {(data ?? []).map((plan: Plan) => (
          <Card key={plan.tier}>
            <CardHeader title={plan.name} subtitle={plan.tagline ?? undefined} />
            <CardBody className="space-y-3">
              <div>
                <div className="num text-xl font-semibold text-[var(--text)]">
                  {formatInr(plan.price_monthly_inr)}
                  <span className="text-xs font-normal text-[var(--text-muted)]"> /month</span>
                </div>
                {plan.price_annual_inr > 0 && (
                  <div className="text-xs text-[var(--text-muted)]">
                    {formatInr(plan.price_annual_inr)} billed annually
                  </div>
                )}
              </div>

              {plan.trial_days > 0 && <StatusPill status="trial" />}

              <div>
                <div className="mb-1 text-[0.6875rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
                  {plan.features.length} features
                </div>
                <div className="max-h-32 space-y-0 overflow-y-auto">
                  {Object.entries(plan.limits).slice(0, 5).map(([key, value]) => (
                    <KeyValue key={key} label={key.replace(/_/g, " ")}
                              value={value === -1 ? "∞" : value.toLocaleString("en-IN")} />
                  ))}
                </div>
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
    </div>
  );
}

/* ====================================================================== */
function JobsTab() {
  const client = useQueryClient();
  const [status, setStatus] = useState("");

  const { data: queue } = useQuery({
    queryKey: ["platform", "queue"], queryFn: platformApi.queue,
    refetchInterval: 10_000,
  });
  const { data: jobs, isLoading } = useQuery({
    queryKey: ["platform", "jobs", status],
    queryFn: () => platformApi.jobs({ status, page_size: 50 }),
    refetchInterval: 10_000,
  });
  const { data: schedules } = useQuery({
    queryKey: ["platform", "schedules"], queryFn: platformApi.schedules,
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["platform"] });
  const retry = useMutation({
    mutationFn: (id: number) => platformApi.retryJob(id), onSuccess: invalidate,
  });
  const cancel = useMutation({
    mutationFn: (id: number) => platformApi.cancelJob(id), onSuccess: invalidate,
  });

  return (
    <div className="space-y-4">
      {queue && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <Card><CardBody><Stat label="Queued" value={queue.queued} /></CardBody></Card>
          <Card><CardBody><Stat label="Running" value={queue.running} /></CardBody></Card>
          <Card><CardBody><Stat label="Retrying" value={queue.failed}
                                 tone={queue.failed > 0 ? "loss" : "default"} /></CardBody></Card>
          <Card><CardBody><Stat label="Dead letter" value={queue.dead_letter}
                                 tone={queue.dead_letter > 0 ? "loss" : "default"} /></CardBody></Card>
          <Card><CardBody><Stat label="p95" value={`${queue.p95_duration_ms.toFixed(0)}ms`} /></CardBody></Card>
        </div>
      )}

      <Card>
        <CardHeader title="Recurring schedule"
                    action={<Timer className="h-4 w-4 text-[var(--text-muted)]" />} />
        <DataTable<Schedule>
          rowKey={(r) => r.kind}
          rows={schedules ?? []}
          columns={[
            { key: "kind", header: "Job", render: (r) => (
              <div>
                <div className="text-xs font-medium capitalize text-[var(--text)]">
                  {r.kind.replace(/_/g, " ")}
                </div>
                <div className="text-xs text-[var(--text-muted)]">{r.description}</div>
              </div>
            ) },
            { key: "every", header: "Every", width: "100px", align: "right",
              render: (r) => <span className="num text-xs">{Math.round(r.every_seconds / 60)}m</span> },
            { key: "last", header: "Last run", width: "110px", align: "right",
              render: (r) => <span className="text-xs text-[var(--text-muted)]">{formatWhen(r.last_run_at)}</span> },
            { key: "runs", header: "Runs", width: "70px", align: "right",
              render: (r) => <span className="num text-xs">{r.run_count}</span> },
          ]}
        />
      </Card>

      <div className="flex items-center gap-2">
        <Select value={status} onChange={setStatus}
                options={[
                  { value: "", label: "All jobs" },
                  { value: "queued", label: "Queued" },
                  { value: "running", label: "Running" },
                  { value: "failed", label: "Retrying" },
                  { value: "dead_letter", label: "Dead letter" },
                  { value: "succeeded", label: "Succeeded" },
                ]} />
        <Button onClick={invalidate}><RefreshCw className="h-3.5 w-3.5" /> Refresh</Button>
      </div>

      <Card>
        {isLoading ? <Skeleton className="h-64" /> : (
          <DataTable<BackgroundJob>
            rowKey={(r) => r.id}
            rows={jobs?.items ?? []}
            empty="The queue is empty."
            columns={[
              { key: "id", header: "ID", width: "60px",
                render: (r) => <span className="num text-xs">{r.id}</span> },
              { key: "kind", header: "Kind",
                render: (r) => <span className="text-xs capitalize">{r.kind.replace(/_/g, " ")}</span> },
              { key: "status", header: "Status", width: "110px",
                render: (r) => <StatusPill status={r.status} /> },
              { key: "attempts", header: "Attempts", width: "90px", align: "right",
                render: (r) => <span className="num text-xs">{r.attempts}/{r.max_attempts}</span> },
              { key: "duration", header: "Duration", width: "90px", align: "right",
                render: (r) => <span className="num text-xs">{r.duration_ms.toFixed(0)}ms</span> },
              { key: "error", header: "Error",
                render: (r) => r.error
                  ? <span className="truncate text-xs text-loss" title={r.error}>{r.error.slice(0, 60)}</span>
                  : <span className="text-xs text-[var(--text-muted)]">—</span> },
              {
                key: "actions", header: "", width: "130px", align: "right",
                render: (r) => (
                  <div className="flex justify-end gap-1">
                    {(r.status === "dead_letter") && (
                      <Button onClick={() => retry.mutate(r.id)}>Replay</Button>
                    )}
                    {(r.status === "queued" || r.status === "failed") && (
                      <Button variant="ghost" onClick={() => cancel.mutate(r.id)}>Cancel</Button>
                    )}
                  </div>
                ),
              },
            ]}
          />
        )}
      </Card>
    </div>
  );
}

/* ====================================================================== */
function HealthTab() {
  const client = useQueryClient();
  const { data: health } = useQuery({
    queryKey: ["platform", "readiness"], queryFn: platformApi.readiness,
    refetchInterval: 30_000,
  });
  const { data: metrics } = useQuery({
    queryKey: ["platform", "metrics"], queryFn: () => platformApi.metrics(1440),
  });
  const { data: routes } = useQuery({
    queryKey: ["platform", "routes"], queryFn: () => platformApi.routeMetrics(1440, 15),
  });
  const { data: errors } = useQuery({
    queryKey: ["platform", "errors"], queryFn: () => platformApi.errors(false),
  });

  const resolve = useMutation({
    mutationFn: (fingerprint: string) => platformApi.resolveError(fingerprint),
    onSuccess: () => client.invalidateQueries({ queryKey: ["platform", "errors"] }),
  });

  return (
    <div className="space-y-4">
      {health && (
        <Card>
          <CardHeader title="Readiness"
                      subtitle={`${health.environment} · v${health.version} · up ${Math.round(health.uptime_seconds / 60)}m`}
                      action={<StatusPill status={health.status} />} />
          <CardBody className="space-y-2">
            {health.checks.map((check) => (
              <div key={check.name} className="flex items-center justify-between gap-3 text-xs">
                <div className="flex min-w-0 items-center gap-2">
                  <span className={check.ok ? "text-gain" : check.critical ? "text-loss" : "text-warn"}>
                    {check.ok ? "●" : "●"}
                  </span>
                  <span className="font-medium capitalize text-[var(--text)]">{check.name}</span>
                  {!check.critical && (
                    <span className="text-[0.625rem] text-[var(--text-muted)]">(non-critical)</span>
                  )}
                </div>
                <span className="truncate text-right text-[var(--text-muted)]">{check.detail}</span>
              </div>
            ))}
          </CardBody>
        </Card>
      )}

      {metrics && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <Card><CardBody><Stat label="Requests / 24h" value={metrics.requests.toLocaleString("en-IN")} /></CardBody></Card>
          <Card><CardBody><Stat label="Error rate" value={`${(metrics.error_rate * 100).toFixed(2)}%`}
                                 tone={metrics.error_rate > 0.01 ? "loss" : "gain"} /></CardBody></Card>
          <Card><CardBody><Stat label="p50" value={`${metrics.p50_ms.toFixed(0)}ms`} /></CardBody></Card>
          <Card><CardBody><Stat label="p95" value={`${metrics.p95_ms.toFixed(0)}ms`} /></CardBody></Card>
          <Card><CardBody><Stat label="p99" value={`${metrics.p99_ms.toFixed(0)}ms`} /></CardBody></Card>
        </div>
      )}

      <Card>
        <CardHeader title="Slowest routes" subtitle="By p95 latency, last 24 hours" />
        <DataTable<RouteMetric>
          rowKey={(r) => `${r.method} ${r.route}`}
          rows={routes ?? []}
          empty="No traffic recorded yet."
          columns={[
            { key: "route", header: "Route",
              render: (r) => (
                <code className="text-xs text-[var(--text)]">
                  <span className="text-[var(--text-muted)]">{r.method}</span> {r.route}
                </code>
              ) },
            { key: "count", header: "Calls", width: "80px", align: "right",
              render: (r) => <span className="num text-xs">{r.count.toLocaleString("en-IN")}</span> },
            { key: "avg", header: "Mean", width: "80px", align: "right",
              render: (r) => <span className="num text-xs">{r.avg_ms.toFixed(0)}ms</span> },
            { key: "p95", header: "p95", width: "80px", align: "right",
              render: (r) => <span className="num text-xs">{r.p95_ms.toFixed(0)}ms</span> },
            { key: "errors", header: "Errors", width: "80px", align: "right",
              render: (r) => (
                <span className={r.errors > 0 ? "num text-xs text-loss" : "num text-xs"}>
                  {r.errors}
                </span>
              ) },
          ]}
        />
      </Card>

      <Card>
        <CardHeader title="Unresolved errors"
                    subtitle="Grouped by fingerprint — an error loop cannot flood this table"
                    action={<Bug className="h-4 w-4 text-[var(--text-muted)]" />} />
        <DataTable<TrackedError>
          rowKey={(r) => r.fingerprint}
          rows={errors?.items ?? []}
          empty="No unresolved errors."
          columns={[
            { key: "type", header: "Exception", width: "160px",
              render: (r) => <code className="text-xs text-loss">{r.exc_type}</code> },
            { key: "message", header: "Message",
              render: (r) => (
                <span className="truncate text-xs text-[var(--text)]" title={r.message}>
                  {r.message.slice(0, 90)}
                </span>
              ) },
            { key: "route", header: "Route", width: "180px",
              render: (r) => <code className="text-xs text-[var(--text-muted)]">{r.route ?? "—"}</code> },
            { key: "count", header: "Seen", width: "70px", align: "right",
              render: (r) => <span className="num text-xs">{r.count}</span> },
            { key: "last", header: "Last", width: "100px", align: "right",
              render: (r) => <span className="text-xs text-[var(--text-muted)]">{formatWhen(r.last_seen_at)}</span> },
            { key: "actions", header: "", width: "90px", align: "right",
              render: (r) => (
                <Button onClick={() => resolve.mutate(r.fingerprint)}>Resolve</Button>
              ) },
          ]}
        />
      </Card>
    </div>
  );
}

/* ====================================================================== */
function BackupsTab() {
  const client = useQueryClient();
  const [verification, setVerification] = useState<
    { ok: boolean; detail: string; restore_command: string } | null
  >(null);

  const { data: status } = useQuery({
    queryKey: ["platform", "backup-status"], queryFn: platformApi.backupStatus,
  });
  const { data: backups, isLoading } = useQuery({
    queryKey: ["platform", "backups"], queryFn: platformApi.backups,
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["platform"] });
  const create = useMutation({ mutationFn: platformApi.createBackup, onSuccess: invalidate });
  const verify = useMutation({
    mutationFn: (id: number) => platformApi.verifyBackup(id),
    onSuccess: (result) => setVerification(result),
  });

  return (
    <div className="space-y-4">
      {status && (
        <Card className={status.stale ? "border-warn/30" : undefined}>
          <CardHeader title="Backup status"
                      subtitle={status.directory}
                      action={<HardDrive className="h-4 w-4 text-[var(--text-muted)]" />} />
          <CardBody>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Stat label="Copies held" value={status.backup_count}
                    hint={`retaining ${status.retention_count}`} />
              <Stat label="Latest" value={status.age_hours !== null ? `${status.age_hours.toFixed(1)}h ago` : "never"}
                    tone={status.stale ? "loss" : "gain"} mono={false} />
              <Stat label="Size" value={formatBytes(status.latest_size_bytes)} mono={false} />
              <Stat label="Verified" value={status.latest_verified_at ? "yes" : "not yet"}
                    tone={status.latest_verified_at ? "gain" : "muted"} mono={false} />
            </div>
            {status.stale && (
              <p className="mt-3 flex items-center gap-2 text-xs text-warn">
                <AlertTriangle className="h-3.5 w-3.5" />
                No backup in the last 48 hours. A backup nobody has restored is
                a hypothesis.
              </p>
            )}
            <div className="mt-3">
              <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
                <Database className="h-3.5 w-3.5" />
                {create.isPending ? "Taking backup…" : "Take a backup now"}
              </Button>
            </div>
            {create.error && <div className="mt-2"><ErrorNotice error={create.error} /></div>}
          </CardBody>
        </Card>
      )}

      {verification && (
        <Card className={verification.ok ? "border-gain/30" : "border-loss/30"}>
          <CardHeader title={verification.ok ? "Backup verified" : "Verification failed"}
                      subtitle={verification.detail} />
          <CardBody>
            <p className="mb-2 text-xs text-[var(--text-muted)]">
              Restore is deliberately manual — a one-click restore is a
              one-click way to lose a production database. Run:
            </p>
            <code className="block overflow-x-auto rounded border border-[var(--border)] bg-[var(--bg-subtle)] px-2 py-1.5 text-xs">
              {verification.restore_command}
            </code>
            <div className="mt-2">
              <Button variant="ghost" onClick={() => setVerification(null)}>Dismiss</Button>
            </div>
          </CardBody>
        </Card>
      )}

      <Card>
        {isLoading ? <Skeleton className="h-48" /> : (
          <DataTable<BackupRecord>
            rowKey={(r) => r.id}
            rows={backups ?? []}
            empty="No backups have been taken."
            columns={[
              { key: "when", header: "Taken", width: "120px",
                render: (r) => <span className="text-xs">{formatWhen(r.finished_at)}</span> },
              { key: "location", header: "Location",
                render: (r) => <code className="truncate text-xs text-[var(--text-muted)]">{r.location}</code> },
              { key: "size", header: "Size", width: "90px", align: "right",
                render: (r) => <span className="text-xs">{formatBytes(r.size_bytes)}</span> },
              { key: "rows", header: "Rows", width: "90px", align: "right",
                render: (r) => <span className="num text-xs">{r.row_count.toLocaleString("en-IN")}</span> },
              { key: "status", header: "Status", width: "100px",
                render: (r) => <StatusPill status={r.status} /> },
              { key: "actions", header: "", width: "90px", align: "right",
                render: (r) => <Button onClick={() => verify.mutate(r.id)}>Verify</Button> },
            ]}
          />
        )}
      </Card>
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

  // 404 on this page means "you are not an operator". The API answers 404
  // rather than 403 deliberately, so the console does not confirm its own
  // existence — but the person who genuinely is signed in deserves a clearer
  // explanation than "not found".
  if (status === 404) {
    return (
      <div className="rounded border border-[var(--border)] bg-[var(--bg-subtle)] px-3 py-2 text-xs text-[var(--text-muted)]">
        This console is available to platform operators only.
      </div>
    );
  }

  return (
    <div className="rounded border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss">
      <span className="font-medium">Something went wrong</span> — {message}
    </div>
  );
}
