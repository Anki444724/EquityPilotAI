/**
 * In-memory session + single-flight refresh.
 *
 * The backend rotates the httpOnly `ierp_refresh` cookie on every successful
 * POST /auth/refresh and marks the previous token used. A second caller that
 * still holds the old cookie gets 401 and the whole family is revoked — that
 * is the production login failure: AuthProvider's mount refresh racing page
 * queries (or React remounting the provider) sent the cookie twice.
 *
 * Every refresh in the app — startup restore and 401 retry — goes through
 * `refreshSession`. Concurrent waiters share one in-flight promise, so the
 * cookie is presented once. The refresh token never enters JS-readable
 * storage; only the short-lived access token is held in module memory.
 */

import type { TokenResponse } from "./types";

function isLoopbackHost(host: string): boolean {
  return host === "localhost" || host === "127.0.0.1" || host === "[::1]";
}

function isLoopbackUrl(url: string): boolean {
  try {
    return isLoopbackHost(new URL(url).hostname);
  } catch {
    return false;
  }
}

/**
 * Browser-facing API origin.
 *
 * Production (equitypilot.in) must never call localhost — that is the
 * *user's* machine, not the EC2 host. An empty NEXT_PUBLIC_API_URL means
 * same-origin (`/api/...`), which Next rewrites (or nginx) proxies to the
 * backend. A baked-in localhost URL is also rewritten to same-origin when
 * the page is served from a real hostname, so a stale image cannot keep
 * sending production traffic to 127.0.0.1:8000.
 *
 * Local `next dev` keeps http://localhost:8000 as the fallback.
 */
function resolveApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured === "") return "";
  const fallback = (configured ?? "http://localhost:8000").replace(/\/$/, "");
  if (typeof window !== "undefined") {
    const host = window.location?.hostname ?? "";
    if (host && !isLoopbackHost(host) && isLoopbackUrl(fallback)) {
      return "";
    }
  }
  return fallback;
}

export const API_BASE = resolveApiBase();

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export type SessionTokens = { access_token: string; csrf_token: string };

type SessionListener = (tokens: SessionTokens | null) => void;

let accessToken: string | null = null;
let csrfToken: string | null = null;
let refreshInFlight: Promise<TokenResponse> | null = null;
/**
 * After a refresh is refused (401), further 401s must not hammer /auth/refresh
 * — that is the infinite-loop guard. Cleared on a successful `setSession`
 * (login / magic-link / a later good refresh).
 */
let refreshBlocked = false;
const listeners = new Set<SessionListener>();

/** True in the browser. Refresh uses an httpOnly cookie the server cannot see. */
function isBrowser(): boolean {
  return typeof window !== "undefined";
}

export function currentAccessToken(): string | null {
  return accessToken;
}

export function currentCsrfToken(): string | null {
  return csrfToken;
}

export function subscribeSession(listener: SessionListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function setSession(tokens: SessionTokens | null): void {
  accessToken = tokens?.access_token ?? null;
  csrfToken = tokens?.csrf_token ?? null;
  if (tokens) refreshBlocked = false;
  for (const listener of listeners) listener(tokens);
}

export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  return headers;
}

/** Throw a typed ApiError carrying the server's `detail` when present. */
export async function raise(res: Response): Promise<never> {
  let detail: unknown = res.statusText;
  try {
    detail = (await res.json()).detail ?? res.statusText;
  } catch { /* non-JSON error body */ }
  throw new ApiError(
    res.status,
    typeof detail === "string" ? detail : JSON.stringify(detail),
  );
}

/**
 * Endpoints that establish or destroy a session. A 401 from these is the
 * answer (wrong password, no cookie, already signed out) — not a reason to
 * rotate the refresh cookie.
 */
const NO_REFRESH_PREFIXES = [
  "/api/v1/auth/login",
  "/api/v1/auth/refresh",
  "/api/v1/auth/register",
  "/api/v1/auth/logout",
  "/api/v1/auth/magic-link",
  "/api/v1/auth/verify-email",
  "/api/v1/auth/resend-verification",
  "/api/v1/auth/password-reset",
  "/api/v1/auth/username-available",
  "/api/v1/auth/config",
  "/api/v1/auth/password-policy",
  "/api/v1/auth/oauth",
];

export function shouldAttemptRefresh(path: string): boolean {
  const route = path.split("?")[0] ?? path;
  return !NO_REFRESH_PREFIXES.some(
    (prefix) => route === prefix || route.startsWith(`${prefix}/`),
  );
}

function transport(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init?.headers ?? {}) },
    credentials: "include",
    cache: "no-store",
  });
}

async function performRefresh(): Promise<TokenResponse> {
  const res = await transport("/api/v1/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    if (res.status === 401) {
      refreshBlocked = true;
      setSession(null);
    }
    await raise(res);
  }
  const tokens = (await res.json()) as TokenResponse;
  setSession(tokens);
  return tokens;
}

/**
 * Rotate the session. Concurrent callers share one HTTP request so the
 * httpOnly refresh cookie is never presented twice.
 */
export function refreshSession(): Promise<TokenResponse> {
  if (refreshInFlight) return refreshInFlight;
  if (refreshBlocked) {
    return Promise.reject(
      new ApiError(401, "Session expired. Please sign in again."),
    );
  }
  if (!isBrowser()) {
    return Promise.reject(new ApiError(401, "No session to refresh."));
  }

  const pending = performRefresh();
  refreshInFlight = pending;
  return pending.finally(() => {
    if (refreshInFlight === pending) refreshInFlight = null;
  });
}

/**
 * AuthProvider mount path. Uses the same lock as 401 retries so a page query
 * that discovers an expired access token cannot start a second refresh.
 */
export async function restoreSession(): Promise<void> {
  if (!currentAccessToken()) await refreshSession();
}

/**
 * The single fetch used by every API call.
 *
 * If a refresh is already in flight, authenticated callers wait for it so
 * they go out with the new access token instead of racing a 401. A 401 after
 * that still joins the same lock, retries exactly once, and never retries the
 * refresh request itself.
 */
export async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const mayRefresh = isBrowser() && shouldAttemptRefresh(path);

  if (mayRefresh && refreshInFlight) {
    try {
      await refreshInFlight;
    } catch {
      // Refresh failed; still attempt the original request so the caller
      // sees the real 401 rather than a swallowed error.
    }
  }

  const first = await transport(path, init);
  if (first.status !== 401 || !mayRefresh) return first;
  if (refreshBlocked && !refreshInFlight) return first;

  try {
    await refreshSession();
  } catch {
    return first;
  }
  return transport(path, init);
}

/** Test hook — never call from application code. */
export function resetAuthSessionForTests(): void {
  accessToken = null;
  csrfToken = null;
  refreshInFlight = null;
  refreshBlocked = false;
  listeners.clear();
}

export function isRefreshBlockedForTests(): boolean {
  return refreshBlocked;
}
