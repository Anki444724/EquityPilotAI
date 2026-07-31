/** Typed API client. Single place that knows the backend URL. */
import type {
  CompanyDetail, CompanyProfile, CoverageResponse, DashboardOverview,
  DocCapabilities, DocChunk, DocEntity, DocFact, DocSearchResponse,
  DocStatistics, DocTable, DocumentDetail, DocumentSummary, GraphResponse,
  PaginatedCompanies, SearchResponse, SessionUser, UploadResponse,
  AlertEvent, AlertSummary, Attribution, Portfolio, PortfolioCapabilities,
  PortfolioCommentary, PortfolioView, WatchlistMeta, WatchlistRow,
  GenerateResponse, ReportCapabilities, ReportDetail, ReportJob,
  ReportStatistics, ReportSummary,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Every read in the application goes through here.
 *
 * This used to send no credentials at all, which was invisible while the
 * backend ran with AUTH_DEV_MODE=true and treated every caller as a super
 * admin. Against a production backend that enforces authentication, each of
 * these calls returned 401 and the UI reported it as "cannot reach the API" —
 * a connectivity message for what was really an authentication failure.
 *
 * Credentials are attached in one place so no caller can forget them.
 */
/**
 * The single fetch used by every call in this module.
 *
 * `request()` and `authed()` both delegate here, and so does every raw call
 * that cannot use them (multipart uploads, DELETEs with no body, HTML
 * previews). Centralising it is the point: seven call sites previously
 * called `fetch()` directly and therefore sent no credentials at all, which
 * was invisible while the backend ran with AUTH_DEV_MODE=true and produced
 * "Authentication required." for every one of them in production.
 *
 * `Content-Type` is deliberately not forced here. A multipart body must be
 * allowed to set its own header, including the generated boundary; sending
 * `application/json` alongside a FormData body makes the server fail to parse
 * it.
 */
async function rawFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init?.headers ?? {}) },
    credentials: "include",
    cache: "no-store",
  });
}

/** Throw a typed ApiError carrying the server's `detail` when present. */
async function raise(res: Response): Promise<never> {
  let detail: unknown = res.statusText;
  try {
    detail = (await res.json()).detail ?? res.statusText;
  } catch { /* non-JSON error body */ }
  throw new ApiError(
    res.status,
    typeof detail === "string" ? detail : JSON.stringify(detail),
  );
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await rawFetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) await raise(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string; version: string }>("/health"),

  me: () => request<SessionUser>("/api/v1/auth/me"),

  searchCompanies: (q: string, limit = 20) =>
    request<SearchResponse>(
      `/api/v1/companies/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  listCompanies: (page = 1, pageSize = 25, sector?: string) => {
    const p = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (sector) p.set("sector", sector);
    return request<PaginatedCompanies>(`/api/v1/companies?${p}`);
  },

  sectors: () => request<string[]>("/api/v1/companies/sectors"),

  company: (id: string) => request<CompanyDetail>(`/api/v1/companies/${id}`),

  companyProfile: (id: string) =>
    request<CompanyProfile>(`/api/v1/companies/${id}/profile`),

  dashboard: () => request<DashboardOverview>("/api/v1/dashboard/overview"),
};

/* ---------------------------------------------------------------- Module 2 */
import type {
  CapexResponse, DebtResponse, FinancialsOverview, RatioResponse,
  ShareholdingResponse, StatementResponse, WorkingCapitalResponse,
} from "./types";

const co = (ticker: string) => `/api/v1/company/${encodeURIComponent(ticker)}`;

export const analysisApi = {
  incomeStatement: (t: string) => request<StatementResponse>(`${co(t)}/income-statement`),
  balanceSheet: (t: string) => request<StatementResponse>(`${co(t)}/balance-sheet`),
  cashFlow: (t: string) => request<StatementResponse>(`${co(t)}/cash-flow`),
  financials: (t: string) => request<FinancialsOverview>(`${co(t)}/financials`),
  ratios: (t: string, wacc?: number) =>
    request<RatioResponse>(`${co(t)}/ratios${wacc !== undefined ? `?wacc=${wacc}` : ""}`),
  workingCapital: (t: string) => request<WorkingCapitalResponse>(`${co(t)}/working-capital`),
  capex: (t: string) => request<CapexResponse>(`${co(t)}/capex`),
  debt: (t: string) => request<DebtResponse>(`${co(t)}/debt`),
  shareholding: (t: string) => request<ShareholdingResponse>(`${co(t)}/shareholding`),
};

/* ---------------------------------------------------------------- Module 3 */
import type {
  AssumptionUpdateRequest, ForecastResponse, ScenarioName, ScenarioResponse,
} from "./types";

export const forecastApi = {
  get: (ticker: string, opts: { scenario?: ScenarioName; horizon?: number; method?: string; forecastId?: string } = {}) => {
    const p = new URLSearchParams();
    if (opts.scenario) p.set("scenario", opts.scenario);
    if (opts.horizon) p.set("horizon", String(opts.horizon));
    if (opts.method) p.set("method", opts.method);
    if (opts.forecastId) p.set("forecast_id", opts.forecastId);
    const q = p.toString();
    return request<ForecastResponse>(`${co(ticker)}/forecast${q ? `?${q}` : ""}`);
  },

  scenarios: (ticker: string, horizon?: number, forecastId?: string) => {
    const p = new URLSearchParams();
    if (horizon) p.set("horizon", String(horizon));
    if (forecastId) p.set("forecast_id", forecastId);
    const q = p.toString();
    return request<ScenarioResponse>(`${co(ticker)}/forecast/scenarios${q ? `?${q}` : ""}`);
  },

  updateAssumptions: (ticker: string, body: AssumptionUpdateRequest, forecastId?: string) =>
    request<ForecastResponse>(
      `${co(ticker)}/forecast/assumptions${forecastId ? `?forecast_id=${forecastId}` : ""}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  create: (ticker: string, body: Record<string, unknown>) =>
    request<ForecastResponse>(`${co(ticker)}/forecast`, {
      method: "POST", body: JSON.stringify(body),
    }),
};

/* ---------------------------------------------------------------- Module 4 */
import type { SensitivityOut, SimulationOut, ValuationResponse } from "./types";

export interface ValuationParams {
  scenario?: string; horizon?: number; convention?: string;
  terminal_method?: string; terminal_growth?: number; exit_multiple?: number;
  margin_of_safety?: number; dynamic_wacc?: boolean;
}

export const valuationApi = {
  get: (ticker: string, p: ValuationParams = {}) => {
    const q = new URLSearchParams();
    Object.entries(p).forEach(([k, v]) => {
      if (v !== undefined && v !== null) q.set(k, String(v));
    });
    const s = q.toString();
    return request<ValuationResponse>(`${co(ticker)}/valuation${s ? `?${s}` : ""}`);
  },

  sensitivity: (ticker: string, row: string, col: string, steps = 2, horizon = 5) =>
    request<SensitivityOut>(
      `${co(ticker)}/valuation/sensitivity?row=${row}&col=${col}&steps=${steps}&horizon=${horizon}`,
    ),

  simulation: (ticker: string, trials = 1000, horizon = 5) =>
    request<SimulationOut>(
      `${co(ticker)}/valuation/simulation?trials=${trials}&horizon=${horizon}`,
    ),
};

/* ---------------------------------------------------------------- Module 5 */
import type {
  ExplanationResponse, PeerComparisonResponse, ScoreHistoryResponse,
  ScoreResponse, WeightProfileListResponse, WeightProfileOut,
} from "./types";

export const scoringApi = {
  get: (ticker: string, profile?: string, save = false) => {
    const p = new URLSearchParams();
    if (profile) p.set("profile", profile);
    if (save) p.set("save", "true");
    const q = p.toString();
    return request<ScoreResponse>(`${co(ticker)}/scoring${q ? `?${q}` : ""}`);
  },

  explanation: (ticker: string, profile?: string) =>
    request<ExplanationResponse>(
      `${co(ticker)}/scoring/explanation${profile ? `?profile=${profile}` : ""}`,
    ),

  history: (ticker: string, profile?: string) =>
    request<ScoreHistoryResponse>(
      `${co(ticker)}/scoring/history${profile ? `?profile=${profile}` : ""}`,
    ),

  peers: (ticker: string, profile?: string, limit = 5) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (profile) p.set("profile", profile);
    return request<PeerComparisonResponse>(`${co(ticker)}/scoring/peers?${p}`);
  },

  profiles: () => request<WeightProfileListResponse>("/api/v1/scoring/weights"),

  saveProfile: (body: {
    key: string; label: string; weights: Record<string, number>;
    description?: string; derived_from?: string;
  }) =>
    request<WeightProfileOut>("/api/v1/scoring/weights", {
      method: "PUT", body: JSON.stringify(body),
    }),
};

/* ---------------------------------------------------------------- Module 6 */
import type {
  AIAnalysisResponse, AIChatResponse, AIContextResponse, AIReportResponse,
  AIUsageResponse, CapabilityListResponse, ProviderListResponse, PromptOut,
} from "./types";

export const aiApi = {
  capabilities: () => request<CapabilityListResponse>("/api/v1/ai/capabilities"),
  providers: () => request<ProviderListResponse>("/api/v1/ai/providers"),
  usage: () => request<AIUsageResponse>("/api/v1/ai/usage"),
  prompts: () => request<{ prompts: PromptOut[] }>("/api/v1/ai/prompts"),

  savePrompt: (key: string, body: Record<string, unknown>) =>
    request<PromptOut>(`/api/v1/ai/prompts/${key}`, {
      method: "PUT", body: JSON.stringify(body),
    }),

  analyse: (ticker: string, capability: string, opts: { provider?: string; style?: string } = {}) =>
    request<AIAnalysisResponse>(`${co(ticker)}/ai/analyse`, {
      method: "POST",
      body: JSON.stringify({ capability, save: true, ...opts }),
    }),

  chat: (ticker: string, question: string, sessionId = "default") =>
    request<AIChatResponse>(`${co(ticker)}/ai/chat`, {
      method: "POST",
      body: JSON.stringify({ question, session_id: sessionId }),
    }),

  report: (ticker: string, capabilities?: string[]) =>
    request<AIReportResponse>(`${co(ticker)}/ai/report`, {
      method: "POST",
      body: JSON.stringify({ capabilities: capabilities ?? [] }),
    }),

  context: (ticker: string) => request<AIContextResponse>(`${co(ticker)}/ai/context`),
};

/* ------------------------------------------------------------------ *
 * Module 7 — Document Intelligence
 * ------------------------------------------------------------------ */
const DOCS = "/api/v1/documents";

export const docsApi = {
  capabilities: () => request<DocCapabilities>(`${DOCS}/capabilities`),

  statistics: (companyId?: string) =>
    request<DocStatistics>(
      `${DOCS}/statistics${companyId ? `?company_id=${companyId}` : ""}`,
    ),

  list: (companyId: string, includeSuperseded = true) =>
    request<DocumentSummary[]>(
      `${DOCS}?company_id=${companyId}&include_superseded=${includeSuperseded}`,
    ),

  get: (id: number) => request<DocumentDetail>(`${DOCS}/${id}`),

  chunks: (documentId: number, limit = 200) =>
    request<DocChunk[]>(`${DOCS}/chunks?document_id=${documentId}&limit=${limit}`),

  tables: (documentId: number) =>
    request<DocTable[]>(`${DOCS}/tables?document_id=${documentId}`),

  entities: (companyId: string, kind?: string) =>
    request<DocEntity[]>(
      `${DOCS}/entities?company_id=${companyId}${kind ? `&kind=${kind}` : ""}`,
    ),

  facts: (companyId: string, category?: string) =>
    request<DocFact[]>(
      `${DOCS}/facts?company_id=${companyId}${category ? `&category=${encodeURIComponent(category)}` : ""}`,
    ),

  coverage: (companyId: string) =>
    request<CoverageResponse>(`${DOCS}/coverage?company_id=${companyId}`),

  knowledge: (companyId: string, minConfidence = 0) =>
    request<GraphResponse>(
      `${DOCS}/knowledge?company_id=${companyId}&min_confidence=${minConfidence}`,
    ),

  search: (q: string, companyId?: string, topK = 8) => {
    const p = new URLSearchParams({ q, top_k: String(topK) });
    if (companyId) p.set("company_id", companyId);
    return request<DocSearchResponse>(`${DOCS}/search?${p}`);
  },

  page: (documentId: number, pageNumber: number) =>
    request<{ page_number: number; text: string; text_source: string; ocr_confidence: number | null }>(
      `${DOCS}/${documentId}/pages/${pageNumber}`,
    ),

  reindex: (companyId?: string) =>
    request<{ reindexed_chunks: number; took_ms: number }>(
      `${DOCS}/reindex${companyId ? `?company_id=${companyId}` : ""}`,
      { method: "POST" },
    ),

  remove: (id: number) =>
    rawFetch(`${DOCS}/${id}`, { method: "DELETE" }).then((r) => {
      if (!r.ok) throw new ApiError(r.status, r.statusText);
    }),

  /**
   * Uploads must not set Content-Type: the browser has to add the multipart
   * boundary itself, so this bypasses the JSON `request` helper entirely.
   */
  upload: async (companyId: string, file: File, docType?: string) => {
    const form = new FormData();
    form.append("company_id", companyId);
    form.append("file", file);
    if (docType) form.append("doc_type", docType);
    // No Content-Type: the browser must set multipart/form-data itself so it
    // can append the boundary. rawFetch attaches the bearer token and cookies.
    const res = await rawFetch(`${DOCS}/upload`, { method: "POST", body: form });
    if (!res.ok) await raise(res);
    return (await res.json()) as UploadResponse;
  },
};

/* ------------------------------------------------------------------ *
 * Module 8 — Portfolio Intelligence
 * ------------------------------------------------------------------ */
const PF = "/api/v1/portfolios";

export const portfolioApi = {
  capabilities: () => request<PortfolioCapabilities>(`${PF}/capabilities`),

  list: () => request<Portfolio[]>(PF),

  create: (body: { name: string } & Partial<Portfolio>) =>
    request<Portfolio>(PF, { method: "POST", body: JSON.stringify(body) }),

  update: (id: number, body: Partial<Portfolio>) =>
    request<Portfolio>(`${PF}/${id}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),

  remove: (id: number) =>
    rawFetch(`${PF}/${id}`, { method: "DELETE" }).then((r) => {
      if (!r.ok) throw new ApiError(r.status, r.statusText);
    }),

  view: (id: number) => request<PortfolioView>(`${PF}/${id}`),

  holdings: (id: number) => request<PortfolioView["holdings"]>(`${PF}/${id}/holdings`),

  transactions: (id: number) =>
    request<Record<string, unknown>[]>(`${PF}/${id}/transactions`),

  addTransaction: (id: number, body: Record<string, unknown>) =>
    request<Record<string, unknown>>(`${PF}/${id}/transactions`, {
      method: "POST", body: JSON.stringify(body),
    }),

  deleteTransaction: (id: number, txnId: number) =>
    rawFetch(`${PF}/${id}/transactions/${txnId}`, { method: "DELETE" })
      .then((r) => { if (!r.ok) throw new ApiError(r.status, r.statusText); }),

  performance: (id: number) =>
    request<PortfolioView["performance"]>(`${PF}/${id}/performance`),

  risk: (id: number) => request<PortfolioView["risk"]>(`${PF}/${id}/risk`),

  allocation: (id: number) =>
    request<PortfolioView["allocations"]>(`${PF}/${id}/allocation`),

  attribution: (id: number) => request<Attribution>(`${PF}/${id}/attribution`),

  alerts: (id: number, triggeredOnly = false) =>
    request<AlertSummary>(`${PF}/${id}/alerts?triggered_only=${triggeredOnly}`),

  allAlerts: (status?: string) =>
    request<AlertEvent[]>(`${PF}/alerts${status ? `?status=${status}` : ""}`),

  acknowledge: (alertId: number) =>
    request<AlertEvent>(`/api/v1/alerts/${alertId}/acknowledge`, {
      method: "POST",
    }),

  overrideRule: (id: number, body: Record<string, unknown>) =>
    request<Record<string, unknown>>(`${PF}/${id}/alerts/rules`, {
      method: "POST", body: JSON.stringify(body),
    }),

  snapshot: (id: number) =>
    request<Record<string, unknown>>(`${PF}/${id}/snapshots`, { method: "POST" }),

  setTarget: (id: number, dimension: string, bucketKey: string, weight: number) =>
    request<Record<string, unknown>>(`${PF}/${id}/targets`, {
      method: "PUT",
      body: JSON.stringify({
        dimension, bucket_key: bucketKey, target_weight: weight,
      }),
    }),

  commentary: (id: number) =>
    request<PortfolioCommentary>(`${PF}/${id}/commentary`),
};

export const watchlistApi = {
  list: () => request<WatchlistMeta[]>("/api/v1/watchlists"),

  create: (name: string, description?: string) =>
    request<WatchlistMeta>("/api/v1/watchlists", {
      method: "POST", body: JSON.stringify({ name, description }),
    }),

  rows: (id: number) => request<WatchlistRow[]>(`/api/v1/watchlists/${id}`),

  add: (id: number, body: { ticker: string } & Record<string, unknown>) =>
    request<WatchlistRow>(`/api/v1/watchlists/${id}/entries`, {
      method: "POST", body: JSON.stringify(body),
    }),

  remove: (id: number, entryId: number) =>
    rawFetch(`/api/v1/watchlists/${id}/entries/${entryId}`, {
      method: "DELETE",
    }).then((r) => { if (!r.ok) throw new ApiError(r.status, r.statusText); }),
};

/* ------------------------------------------------------------------ *
 * Module 9 — Report Generator
 * ------------------------------------------------------------------ */
const RP = "/api/v1/reports";

export const reportApi = {
  capabilities: () => request<ReportCapabilities>(`${RP}/capabilities`),

  statistics: () => request<ReportStatistics>(`${RP}/statistics`),

  jobs: () => request<ReportJob[]>(`${RP}/jobs`),

  list: (companyId?: string, reportType?: string) => {
    const p = new URLSearchParams();
    if (companyId) p.set("company_id", companyId);
    if (reportType) p.set("report_type", reportType);
    return request<ReportSummary[]>(`${RP}?${p}`);
  },

  get: (id: number, includeDocument = false) =>
    request<ReportDetail>(`${RP}/${id}?include_document=${includeDocument}`),

  versions: (id: number) => request<ReportSummary[]>(`${RP}/${id}/versions`),

  generate: (body: {
    company_id: string; report_type: string; formats: string[];
    theme?: string; analyst?: string; portfolio_id?: number | null;
    include_ai?: boolean; use_cache?: boolean;
  }) =>
    request<GenerateResponse>(`${RP}/generate`, {
      method: "POST", body: JSON.stringify(body),
    }),

  remove: (id: number) =>
    rawFetch(`${RP}/${id}`, { method: "DELETE" }).then((r) => {
      if (!r.ok) throw new ApiError(r.status, r.statusText);
    }),

  /** Absolute URLs: the browser navigates to these, it does not fetch them. */
  downloadUrl: (id: number, fmt: string) =>
    `${API_BASE}${RP}/${id}/download/${fmt}`,

  previewUrl: (id: number) => `${API_BASE}${RP}/${id}/preview`,

  preview: async (id: number): Promise<string> => {
    const res = await rawFetch(`${RP}/${id}/preview`);
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    return res.text();
  },
};

/* ------------------------------------------------------------------ *
 * Module 10 — Commercial SaaS platform layer
 *
 * Three clients, split the way the API is: `authApi` for the session,
 * `adminApi` for a tenant administering itself, `platformApi` for the
 * operator console. The split mirrors the permission boundary, so a
 * component reaching for the wrong one is visible in the import.
 * ------------------------------------------------------------------ */
import type {
  ApiKey, AuditRow, AuditSummary, AuthConfig, BackgroundJob, BackupRecord,
  BackupStatus, Entitlements, HealthReport, IssuedApiKey, MetricsOverview,
  Page, Plan, PlatformOverview, PlatformTenant, PlatformUser,
  PlatformUserDetail, QueueDepth, RbacMatrix, RouteMetric, Schedule,
  SessionUserFull, StorageReport, Subscription, TenantDetail,
  TenantOverview, TokenResponse, TrackedError, UsageOverview, UsageSeries,
} from "./types";

/**
 * The access token for the current session.
 *
 * Held in memory rather than in localStorage: a token in localStorage is
 * readable by any script on the page, which turns an XSS bug into a stolen
 * session. The refresh token is an httpOnly cookie the browser sends
 * automatically and script cannot read, so a page reload recovers the session
 * through `/auth/refresh` without the access token ever being persisted.
 */
let accessToken: string | null = null;
let csrfToken: string | null = null;

export function setSession(tokens: { access_token: string; csrf_token: string } | null) {
  accessToken = tokens?.access_token ?? null;
  csrfToken = tokens?.csrf_token ?? null;
}

export function currentAccessToken(): string | null {
  return accessToken;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  return headers;
}

/**
 * Retained as a distinct name because the session calls read better as
 * `authed(...)`, but there is now exactly one code path: every request in
 * this module carries credentials, so no future endpoint can be added
 * without them by accident.
 */
const authed = request;

function query(params: Record<string, string | number | boolean | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

/* ------------------------------------------------------------- session */
export const authApi = {
  config: () => request<AuthConfig>("/api/v1/auth/config"),
  me: () => authed<SessionUserFull>("/api/v1/auth/me"),
  passwordPolicy: () =>
    request<{ min_length: number; passphrase_length: number; requires: string[]; message: string }>(
      "/api/v1/auth/password-policy",
    ),

  /** `identifier` may be an email address or a username. */
  login: async (identifier: string, password: string, rememberMe = false) => {
    const tokens = await authed<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ identifier, password, remember_me: rememberMe }),
    });
    setSession(tokens);
    return tokens;
  },

  usernameAvailable: (username: string) =>
    request<{ username: string; available: boolean; problems: string[] }>(
      `/api/v1/auth/username-available?username=${encodeURIComponent(username)}`,
    ),

  register: (body: {
    email: string; password: string; name: string;
    username?: string; confirm_password?: string; organisation?: string;
  }) =>
    authed<{ message: string; dev_link: string | null }>("/api/v1/auth/register", {
      method: "POST", body: JSON.stringify(body),
    }),

  refresh: async () => {
    const tokens = await authed<TokenResponse>("/api/v1/auth/refresh", { method: "POST" });
    setSession(tokens);
    return tokens;
  },

  logout: async () => {
    try {
      await authed<{ message: string }>("/api/v1/auth/logout", { method: "POST" });
    } finally {
      // Cleared even if the call fails: a client that believes it is still
      // signed in after the user pressed sign out is the worse outcome.
      setSession(null);
    }
  },

  magicLink: (email: string) =>
    authed<{ message: string; dev_link: string | null }>("/api/v1/auth/magic-link", {
      method: "POST", body: JSON.stringify({ email }),
    }),

  consumeMagicLink: async (token: string) => {
    const tokens = await authed<TokenResponse>("/api/v1/auth/magic-link/consume", {
      method: "POST", body: JSON.stringify({ token }),
    });
    setSession(tokens);
    return tokens;
  },

  verifyEmail: (token: string) =>
    authed<{ message: string }>("/api/v1/auth/verify-email", {
      method: "POST", body: JSON.stringify({ token }),
    }),

  requestPasswordReset: (email: string) =>
    authed<{ message: string; dev_link: string | null }>("/api/v1/auth/password-reset", {
      method: "POST", body: JSON.stringify({ email }),
    }),

  confirmPasswordReset: (token: string, password: string) =>
    authed<{ message: string }>("/api/v1/auth/password-reset/confirm", {
      method: "POST", body: JSON.stringify({ token, password }),
    }),

  changePassword: (current_password: string, new_password: string) =>
    authed<{ message: string }>("/api/v1/auth/password", {
      method: "POST", body: JSON.stringify({ current_password, new_password }),
    }),

  sessions: () =>
    authed<{
      session_id: string; issued_at: string | null; expires_at: string | null;
      ip_address: string | null; user_agent: string | null; current: boolean;
    }[]>("/api/v1/auth/sessions"),

  revokeSessions: () =>
    authed<{ message: string }>("/api/v1/auth/sessions", { method: "DELETE" }),

  oauthStart: (provider: string) =>
    authed<{ authorize_url: string; state: string }>(`/api/v1/auth/oauth/${provider}`),
};

/* ------------------------------------------------- tenant administration */
const AD = "/api/v1/admin";

export const adminApi = {
  overview: () => authed<TenantOverview>(`${AD}/overview`),
  rbac: () => authed<RbacMatrix>(`${AD}/rbac`),

  organisation: () => authed<TenantDetail>(`${AD}/organisation`),
  updateOrganisation: (body: Record<string, unknown>) =>
    authed<PlatformTenant>(`${AD}/organisation`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  updateSettings: (settings: Record<string, unknown>) =>
    authed<TenantDetail>(`${AD}/organisation/settings`, {
      method: "PUT", body: JSON.stringify({ settings }),
    }),

  members: (params: {
    role?: string; status?: string; search?: string;
    page?: number; page_size?: number; sort?: string; order?: string;
  } = {}) => authed<Page<PlatformUser>>(`${AD}/members${query(params)}`),

  member: (id: string) => authed<PlatformUserDetail>(`${AD}/members/${id}`),

  invite: (body: { email: string; name: string; role: string }) =>
    authed<PlatformUser>(`${AD}/members`, {
      method: "POST", body: JSON.stringify(body),
    }),

  setMemberRole: (id: string, role: string) =>
    authed<PlatformUser>(`${AD}/members/${id}/role`, {
      method: "PATCH", body: JSON.stringify({ role }),
    }),

  setMemberStatus: (id: string, status: string) =>
    authed<PlatformUser>(`${AD}/members/${id}/status`, {
      method: "PATCH", body: JSON.stringify({ status }),
    }),

  removeMember: (id: string) =>
    authed<void>(`${AD}/members/${id}`, { method: "DELETE" }),

  apiKeys: (includeRevoked = false) =>
    authed<ApiKey[]>(`${AD}/api-keys${query({ include_revoked: includeRevoked })}`),

  createApiKey: (body: { name: string; role: string; expires_in_days: number }) =>
    authed<IssuedApiKey>(`${AD}/api-keys`, {
      method: "POST", body: JSON.stringify(body),
    }),

  revokeApiKey: (id: number) =>
    authed<ApiKey>(`${AD}/api-keys/${id}`, { method: "DELETE" }),

  subscription: () => authed<Subscription>(`${AD}/subscription`),
  changePlan: (tier: string, billing_period = "monthly") =>
    authed<Subscription>(`${AD}/subscription`, {
      method: "POST", body: JSON.stringify({ tier, billing_period }),
    }),
  cancelSubscription: (immediately = false) =>
    authed<Subscription>(`${AD}/subscription${query({ immediately })}`, {
      method: "DELETE",
    }),

  entitlements: () => authed<Entitlements>(`${AD}/entitlements`),
  usage: (days = 30) => authed<UsageOverview>(`${AD}/usage${query({ days })}`),
  usageSeries: (quota: string, days = 30) =>
    authed<UsageSeries>(`${AD}/usage/series${query({ quota, days })}`),

  audit: (params: {
    action?: string; category?: string; severity?: string; outcome?: string;
    search?: string; days?: number; page?: number; page_size?: number;
  } = {}) => authed<Page<AuditRow>>(`${AD}/audit${query(params)}`),

  auditSummary: (days = 7) =>
    authed<AuditSummary>(`${AD}/audit/summary${query({ days })}`),

  storage: () => authed<StorageReport>(`${AD}/storage`),

  jobs: (params: { kind?: string; status?: string; page?: number; page_size?: number } = {}) =>
    authed<Page<BackgroundJob>>(`${AD}/jobs${query(params)}`),

  notifications: (unreadOnly = false) =>
    authed<{
      id: number; topic: string; subject: string; body: string;
      link: string | null; channel: string; read_at: string | null;
      sent_at: string | null; created_at: string;
    }[]>(`${AD}/notifications${query({ unread_only: unreadOnly })}`),
};

/* --------------------------------------------------- operator console */
const PLATFORM = "/api/v1/platform";

export const platformApi = {
  overview: () => authed<PlatformOverview>(`${PLATFORM}/overview`),

  tenants: (params: {
    status?: string; search?: string; page?: number; page_size?: number;
    sort?: string; order?: string;
  } = {}) => authed<Page<PlatformTenant>>(`${PLATFORM}/tenants${query(params)}`),

  tenant: (id: number) => authed<TenantDetail>(`${PLATFORM}/tenants/${id}`),

  createTenant: (body: { name: string; tier: string; slug?: string; industry?: string }) =>
    authed<PlatformTenant>(`${PLATFORM}/tenants`, {
      method: "POST", body: JSON.stringify(body),
    }),

  suspendTenant: (id: number, reason: string) =>
    authed<PlatformTenant>(`${PLATFORM}/tenants/${id}/suspend`, {
      method: "POST", body: JSON.stringify({ reason }),
    }),

  reactivateTenant: (id: number) =>
    authed<PlatformTenant>(`${PLATFORM}/tenants/${id}/reactivate`, { method: "POST" }),

  overrideSubscription: (id: number, body: Record<string, unknown>) =>
    authed<Subscription>(`${PLATFORM}/tenants/${id}/subscription`, {
      method: "PATCH", body: JSON.stringify(body),
    }),

  users: (params: {
    tenant_id?: number; role?: string; status?: string; search?: string;
    page?: number; page_size?: number;
  } = {}) => authed<Page<PlatformUser>>(`${PLATFORM}/users${query(params)}`),

  // Public: this is the pricing page's data source.
  plans: (publicOnly = false) =>
    request<Plan[]>(`${PLATFORM}/plans${query({ public_only: publicOnly })}`),

  updatePlan: (tier: string, body: Record<string, unknown>) =>
    authed<Plan>(`${PLATFORM}/plans/${tier}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),

  audit: (params: {
    tenant_id?: number; action?: string; category?: string; severity?: string;
    outcome?: string; search?: string; days?: number; page?: number; page_size?: number;
  } = {}) => authed<Page<AuditRow>>(`${PLATFORM}/audit${query(params)}`),

  errors: (resolved = false, page = 1) =>
    authed<Page<TrackedError>>(`${PLATFORM}/errors${query({ resolved, page })}`),

  resolveError: (fingerprint: string) =>
    authed<TrackedError>(`${PLATFORM}/errors/${fingerprint}/resolve`, { method: "POST" }),

  metrics: (minutes = 60) =>
    authed<MetricsOverview>(`${PLATFORM}/metrics${query({ minutes })}`),

  routeMetrics: (minutes = 60, limit = 20) =>
    authed<RouteMetric[]>(`${PLATFORM}/metrics/routes${query({ minutes, limit })}`),

  metricsTimeseries: (minutes = 60) =>
    authed<{ at: string; requests: number; errors: number; avg_ms: number }[]>(
      `${PLATFORM}/metrics/timeseries${query({ minutes })}`,
    ),

  jobs: (params: {
    tenant_id?: number; kind?: string; status?: string; page?: number; page_size?: number;
  } = {}) => authed<Page<BackgroundJob>>(`${PLATFORM}/jobs${query(params)}`),

  enqueueJob: (kind: string, payload: Record<string, unknown> = {}) =>
    authed<BackgroundJob>(`${PLATFORM}/jobs`, {
      method: "POST", body: JSON.stringify({ kind, payload }),
    }),

  retryJob: (id: number) =>
    authed<BackgroundJob>(`${PLATFORM}/jobs/${id}/retry`, { method: "POST" }),

  cancelJob: (id: number) =>
    authed<BackgroundJob>(`${PLATFORM}/jobs/${id}/cancel`, { method: "POST" }),

  queue: () => authed<QueueDepth>(`${PLATFORM}/queue`),
  schedules: () => authed<Schedule[]>(`${PLATFORM}/schedules`),

  backups: () => authed<BackupRecord[]>(`${PLATFORM}/backups`),
  backupStatus: () => authed<BackupStatus>(`${PLATFORM}/backups/status`),
  createBackup: () => authed<BackupRecord>(`${PLATFORM}/backups`, { method: "POST" }),
  verifyBackup: (id: number) =>
    authed<{ backup_id: number; ok: boolean; detail: string; restore_command: string }>(
      `${PLATFORM}/backups/${id}/verify`, { method: "POST" },
    ),

  readiness: () => authed<HealthReport>(`${PLATFORM}/readiness`),
};

/* ------------------------------------------------------------- system */
export const systemApi = {
  live: () => request<{ status: string; version: string; uptime_seconds: number }>("/health/live"),
  ready: () => request<HealthReport>("/health/ready"),
  metrics: () => request<Record<string, unknown>>("/metrics"),
};
