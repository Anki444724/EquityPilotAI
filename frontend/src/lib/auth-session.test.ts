/**
 * Single-flight refresh: the production login failure is a second
 * POST /auth/refresh presenting a cookie the backend has already rotated.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  apiFetch,
  currentAccessToken,
  refreshSession,
  resetAuthSessionForTests,
  restoreSession,
  setSession,
  shouldAttemptRefresh,
  subscribeSession,
} from "./auth-session";
import { authApi } from "./api";

type FetchInput = Parameters<typeof fetch>[0];
type FetchInit = Parameters<typeof fetch>[1];

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function header(init: FetchInit, name: string): string | null {
  if (!init?.headers) return null;
  if (init.headers instanceof Headers) return init.headers.get(name);
  const rec = init.headers as Record<string, string>;
  return rec[name] ?? rec[name.toLowerCase()] ?? null;
}

function tokens(access: string, csrf = `csrf-${access}`) {
  return {
    access_token: access,
    token_type: "bearer",
    expires_in: 900,
    csrf_token: csrf,
    refresh_token: null,
  };
}

let calls: { url: string; init?: FetchInit }[];

function installFetch(
  handler: (url: string, init?: FetchInit) => Promise<Response> | Response,
) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: FetchInput, init?: FetchInit) => {
      const url = String(input);
      calls.push({ url, init });
      return handler(url, init);
    }),
  );
}

beforeEach(() => {
  resetAuthSessionForTests();
  vi.stubGlobal("window", globalThis);
  vi.stubGlobal("localStorage", {
    getItem: vi.fn(),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  });
  vi.stubGlobal("sessionStorage", {
    getItem: vi.fn(),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAuthSessionForTests();
});

describe("API_BASE", () => {
  it("never leaves a production hostname pointing at localhost", async () => {
    vi.resetModules();
    vi.stubGlobal("window", {
      location: { hostname: "equitypilot.in", href: "https://equitypilot.in/" },
    });
    const { API_BASE } = await import("./auth-session");
    expect(API_BASE === "" || !API_BASE.includes("localhost")).toBe(true);
    expect(API_BASE.includes("127.0.0.1")).toBe(false);
  });
});

describe("shouldAttemptRefresh", () => {
  it("refuses to refresh login, refresh, and other session-establishing routes", () => {
    expect(shouldAttemptRefresh("/api/v1/auth/login")).toBe(false);
    expect(shouldAttemptRefresh("/api/v1/auth/refresh")).toBe(false);
    expect(shouldAttemptRefresh("/api/v1/auth/register")).toBe(false);
    expect(shouldAttemptRefresh("/api/v1/auth/logout")).toBe(false);
    expect(shouldAttemptRefresh("/api/v1/auth/password-reset")).toBe(false);
    expect(shouldAttemptRefresh("/api/v1/auth/password-reset/confirm")).toBe(false);
    expect(shouldAttemptRefresh("/api/v1/auth/oauth/google")).toBe(false);
  });

  it("does refresh authenticated routes including /auth/me", () => {
    expect(shouldAttemptRefresh("/api/v1/auth/me")).toBe(true);
    expect(shouldAttemptRefresh("/api/v1/auth/sessions")).toBe(true);
    expect(shouldAttemptRefresh("/api/v1/dashboard/overview")).toBe(true);
  });
});

describe("single-flight refresh", () => {
  it("concurrent callers issue exactly one /auth/refresh", async () => {
    let inflight = 0;
    let peak = 0;
    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) {
        inflight += 1;
        peak = Math.max(peak, inflight);
        await new Promise((r) => setTimeout(r, 20));
        inflight -= 1;
        return jsonResponse(200, tokens("rotated-1"));
      }
      return jsonResponse(404, { detail: "no" });
    });

    const [a, b, c] = await Promise.all([
      refreshSession(),
      refreshSession(),
      refreshSession(),
    ]);

    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(1);
    expect(peak).toBe(1);
    expect(a.access_token).toBe("rotated-1");
    expect(b.access_token).toBe("rotated-1");
    expect(c.access_token).toBe("rotated-1");
    expect(currentAccessToken()).toBe("rotated-1");
  });

  it("never writes a refresh token to localStorage or sessionStorage", async () => {
    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) return jsonResponse(200, tokens("mem-only"));
      return jsonResponse(404, {});
    });
    await refreshSession();
    expect(localStorage.setItem).not.toHaveBeenCalled();
    expect(sessionStorage.setItem).not.toHaveBeenCalled();
    expect(currentAccessToken()).toBe("mem-only");
  });

  it("successful rotation stores the new access token for later requests", async () => {
    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) return jsonResponse(200, tokens("after-rotate"));
      return jsonResponse(200, { ok: true });
    });
    await refreshSession();
    await apiFetch("/api/v1/dashboard/overview");
    const dash = calls.find((c) => c.url.includes("/dashboard"));
    expect(header(dash?.init, "Authorization")).toBe("Bearer after-rotate");
  });
});

describe("401 interceptor", () => {
  it("waiting requests all retry with the new access token", async () => {
    installFetch(async (url, init) => {
      if (url.includes("/auth/refresh")) {
        await new Promise((r) => setTimeout(r, 15));
        return jsonResponse(200, tokens("fresh-access"));
      }
      const auth = header(init, "Authorization");
      if (auth === "Bearer fresh-access") {
        return jsonResponse(200, { ok: true, path: url });
      }
      return jsonResponse(401, { detail: "Authentication required." });
    });

    const responses = await Promise.all([
      apiFetch("/api/v1/dashboard/overview"),
      apiFetch("/api/v1/companies?page=1"),
      apiFetch("/api/v1/auth/me"),
    ]);

    expect(responses.every((r) => r.status === 200)).toBe(true);
    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(1);
    const retried = calls.filter((c) =>
      !c.url.includes("/auth/refresh") &&
      header(c.init, "Authorization") === "Bearer fresh-access",
    );
    expect(retried.length).toBeGreaterThanOrEqual(3);
    expect(currentAccessToken()).toBe("fresh-access");
  });

  it("a failed refresh does not loop", async () => {
    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) {
        return jsonResponse(401, { detail: "No session to refresh." });
      }
      return jsonResponse(401, { detail: "Authentication required." });
    });

    const first = await apiFetch("/api/v1/dashboard/overview");
    const second = await apiFetch("/api/v1/companies");
    const third = await apiFetch("/api/v1/auth/me");

    expect(first.status).toBe(401);
    expect(second.status).toBe(401);
    expect(third.status).toBe(401);
    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(1);
    expect(currentAccessToken()).toBeNull();

    await expect(refreshSession()).rejects.toBeInstanceOf(ApiError);
    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(1);
  });

  it("does not refresh on a failed login", async () => {
    installFetch(async (url) => {
      if (url.includes("/auth/login")) {
        return jsonResponse(401, { detail: "Incorrect email or password." });
      }
      if (url.includes("/auth/refresh")) {
        return jsonResponse(200, tokens("should-not-happen"));
      }
      return jsonResponse(404, {});
    });

    const res = await apiFetch("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ identifier: "a", password: "b" }),
    });
    expect(res.status).toBe(401);
    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(0);
    expect(currentAccessToken()).toBeNull();
  });
});

describe("auth-provider startup cannot race API refresh", () => {
  it("restoreSession and apiFetch share one refresh lock", async () => {
    let refreshCount = 0;
    installFetch(async (url, init) => {
      if (url.includes("/auth/refresh")) {
        refreshCount += 1;
        await new Promise((r) => setTimeout(r, 20));
        return jsonResponse(200, tokens("startup-token"));
      }
      if (header(init, "Authorization") === "Bearer startup-token") {
        return jsonResponse(200, { id: "u1" });
      }
      return jsonResponse(401, { detail: "Authentication required." });
    });

    const [restored, dash, me] = await Promise.all([
      restoreSession(),
      apiFetch("/api/v1/dashboard/overview"),
      apiFetch("/api/v1/auth/me"),
    ]);

    expect(restored).toBeUndefined();
    expect(dash.status).toBe(200);
    expect(me.status).toBe(200);
    expect(refreshCount).toBe(1);
    expect(currentAccessToken()).toBe("startup-token");
  });

  it("authApi.refresh is the same single-flight function", () => {
    expect(authApi.refresh).toBe(refreshSession);
  });

  it("AuthProvider restores through the shared lock, not a private refresh", async () => {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    const { dirname, join } = await import("node:path");
    const here = dirname(fileURLToPath(import.meta.url));
    const src = readFileSync(
      join(here, "../components/layout/auth-provider.tsx"),
      "utf8",
    );
    expect(src).toContain("restoreSession");
    expect(src).toContain("subscribeSession");
    expect(src).not.toMatch(/authApi\.refresh\s*\(/);
  });

  it("a failed startup refresh notifies listeners once and does not retry", async () => {
    const seen: Array<string | null> = [];
    subscribeSession((session) => {
      seen.push(session ? session.access_token : null);
    });
    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) {
        return jsonResponse(401, { detail: "No session to refresh." });
      }
      return jsonResponse(401, { detail: "Authentication required." });
    });

    await expect(restoreSession()).rejects.toBeInstanceOf(ApiError);
    const dash = await apiFetch("/api/v1/dashboard/overview");
    expect(dash.status).toBe(401);
    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(1);
    expect(seen.filter((s) => s === null)).toHaveLength(1);
  });
});

describe("session helpers", () => {
  it("setSession after a failed refresh allows a later login to refresh again", async () => {
    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) {
        return jsonResponse(401, { detail: "No session to refresh." });
      }
      return jsonResponse(401, {});
    });
    await expect(refreshSession()).rejects.toBeInstanceOf(ApiError);

    setSession({ access_token: "from-login", csrf_token: "csrf" });

    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) return jsonResponse(200, tokens("after-login"));
      return jsonResponse(200, {});
    });
    const rotated = await refreshSession();
    expect(rotated.access_token).toBe("after-login");
  });

  it("does not refresh during SSR (no window)", async () => {
    vi.unstubAllGlobals();
    resetAuthSessionForTests();
    expect(typeof window).toBe("undefined");

    installFetch(async (url) => {
      if (url.includes("/auth/refresh")) return jsonResponse(200, tokens("ssr"));
      return jsonResponse(401, { detail: "Authentication required." });
    });

    const res = await apiFetch("/api/v1/dashboard/overview");
    expect(res.status).toBe(401);
    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(0);
    await expect(refreshSession()).rejects.toBeInstanceOf(ApiError);
    expect(calls.filter((c) => c.url.includes("/auth/refresh"))).toHaveLength(0);
  });
});
