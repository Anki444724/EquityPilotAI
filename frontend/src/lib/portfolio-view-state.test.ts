import { describe, expect, it } from "vitest";
import { ApiError } from "./auth-session";
import {
  describeError, resolvePortfolioListState, type PortfolioListInput,
} from "./portfolio-view-state";
import type { Portfolio } from "./types";

const portfolio = { id: 1, name: "Core equity" } as unknown as Portfolio;

function input(overrides: Partial<PortfolioListInput> = {}): PortfolioListInput {
  return {
    authInitialising: false,
    isAuthenticated: true,
    isPending: false,
    isFetching: false,
    data: undefined,
    error: null,
    ...overrides,
  };
}

describe("portfolio list state", () => {
  it("is loading while the session is still being restored", () => {
    // Even for a user who will turn out to be signed out: a sign-in flash on
    // every reload is what the shared refresh lock exists to avoid.
    expect(resolvePortfolioListState(input({
      authInitialising: true, isAuthenticated: false,
    }))).toEqual({ kind: "loading" });
  });

  it("is unauthenticated once the session settles with no user", () => {
    expect(resolvePortfolioListState(input({ isAuthenticated: false })))
      .toEqual({ kind: "unauthenticated" });
  });

  it("is loading while the request is in flight", () => {
    expect(resolvePortfolioListState(input({ isPending: true })))
      .toEqual({ kind: "loading" });
  });

  it("never reports 'empty' before the answer arrives", () => {
    // The reported bug: "No portfolios yet" printed under "Loading…".
    const state = resolvePortfolioListState(input({
      isPending: true, isFetching: true,
    }));
    expect(state.kind).toBe("loading");
    expect(state.kind).not.toBe("empty");
  });

  it("reports an empty list as empty, not as an error", () => {
    expect(resolvePortfolioListState(input({ data: [] })))
      .toEqual({ kind: "empty" });
  });

  it("reports portfolios when there are some", () => {
    expect(resolvePortfolioListState(input({ data: [portfolio] })))
      .toEqual({ kind: "ready", portfolios: [portfolio] });
  });

  it("distinguishes an expired session from an API failure", () => {
    expect(resolvePortfolioListState(input({
      error: new ApiError(401, "Authentication required."),
    }))).toEqual({ kind: "session-expired" });

    expect(resolvePortfolioListState(input({
      error: new ApiError(500, "boom"),
    }))).toEqual({
      kind: "error", message: "The API returned HTTP 500: boom",
    });
  });

  it("keeps showing data during a background refetch", () => {
    expect(resolvePortfolioListState(input({
      data: [portfolio], isFetching: true,
    }))).toEqual({ kind: "ready", portfolios: [portfolio] });
  });

  it("prefers the error over stale data so failures are visible", () => {
    expect(resolvePortfolioListState(input({
      data: [portfolio], error: new ApiError(503, "unavailable"),
    })).kind).toBe("error");
  });
});

describe("describeError", () => {
  it("never calls a 401 an unreachable API", () => {
    expect(describeError(new ApiError(401, "Authentication required.")))
      .not.toContain("Cannot reach");
  });

  it("reports a genuine transport failure as unreachable", () => {
    expect(describeError(new TypeError("Failed to fetch")))
      .toContain("Cannot reach the API");
  });
});
