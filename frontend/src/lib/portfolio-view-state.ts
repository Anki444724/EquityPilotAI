/**
 * What the portfolio list is currently saying.
 *
 * The page used to derive this inline from four booleans, and got it wrong in
 * the way that produced the reported screen: the header printed "Loading…"
 * whenever no summary was resolved — including for a user who simply owns no
 * portfolios — while the body printed "No portfolios yet" underneath it. Two
 * contradictory statements, neither of them a fact about the request.
 *
 * The rules are small but they have to hold together, so they live here as one
 * pure function with one return type. Six states, mutually exclusive:
 *
 *   unauthenticated   no session; the shell shows sign-in
 *   loading           the answer is not known yet
 *   session-expired   the API refused with 401 after a refresh attempt
 *   error             the API failed for any other reason
 *   empty             authenticated, answered, and the user owns nothing
 *   ready             authenticated, answered, and there is something to show
 *
 * An empty list is an answer, not a failure — the distinction Task 6 asks for.
 */

import { ApiError } from "./auth-session";
import type { Portfolio } from "./types";

export type PortfolioListState =
  | { kind: "unauthenticated" }
  | { kind: "loading" }
  | { kind: "session-expired" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | { kind: "ready"; portfolios: Portfolio[] };

export interface PortfolioListInput {
  /** AuthProvider has not finished its startup refresh yet. */
  authInitialising: boolean;
  isAuthenticated: boolean;
  /** React Query: no data and no error yet. */
  isPending: boolean;
  isFetching: boolean;
  data: Portfolio[] | undefined;
  error: unknown;
}

export function resolvePortfolioListState(
  input: PortfolioListInput,
): PortfolioListState {
  // Until the session settles nothing is known — least of all whether the
  // user has portfolios. Checked before authentication so a signed-in user
  // reloading the page never sees a sign-in flash.
  if (input.authInitialising) return { kind: "loading" };

  if (!input.isAuthenticated) return { kind: "unauthenticated" };

  if (input.error !== null && input.error !== undefined) {
    if (input.error instanceof ApiError && input.error.status === 401) {
      return { kind: "session-expired" };
    }
    return { kind: "error", message: describeError(input.error) };
  }

  // No data and no error: either the query has not run or it is in flight.
  if (input.data === undefined) return { kind: "loading" };

  // Data in hand. A background refetch must not send the page back to a
  // skeleton — it already has an answer to show.
  if (input.data.length === 0) return { kind: "empty" };

  return { kind: "ready", portfolios: input.data };
}

/** A 401 is never "cannot reach the API"; that conflation is Task 6's point. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return `The API returned HTTP ${error.status}: ${error.message}`;
  }
  return "Cannot reach the API. Check your connection and try again.";
}
