"use client";

/**
 * Session state for the whole application.
 *
 * The access token is deliberately held in memory rather than localStorage: a
 * token in localStorage is readable by any script that manages to run on the
 * page. Durability across reloads comes from the refresh token, which the
 * backend sets as an httpOnly cookie the browser sends automatically — so on
 * mount we simply ask /auth/refresh whether there is a live session.
 */

import { ApiError, authApi, currentAccessToken, setSession } from "@/lib/api";
import type { SessionUserFull } from "@/lib/types";
import { useQueryClient } from "@tanstack/react-query";
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";

interface AuthState {
  user: SessionUserFull | null;
  /** True until the initial refresh attempt settles, so the UI can avoid
   *  flashing a sign-in screen at a user who is already signed in. */
  initialising: boolean;
  signIn: (identifier: string, password: string, rememberMe?: boolean) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUserFull | null>(null);
  const [initialising, setInitialising] = useState(true);
  const queryClient = useQueryClient();

  // Restore a session on first load. A 401 here is the normal "not signed in"
  // answer, not an error worth surfacing.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (!currentAccessToken()) await authApi.refresh();
        const me = await authApi.me();
        if (!cancelled) {
          setUser(me);
          await queryClient.invalidateQueries();
        }
      } catch {
        if (!cancelled) {
          setSession(null);
          setUser(null);
        }
      } finally {
        if (!cancelled) setInitialising(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const signIn = useCallback(async (
    identifier: string, password: string, rememberMe = false,
  ) => {
    await authApi.login(identifier, password, rememberMe);
    setUser(await authApi.me());
    // Every query that ran while the user was anonymous resolved to a 401 and
    // is now cached as an error. React Query will not retry them on its own,
    // so the dashboard would sit empty until a manual reload. Clearing the
    // cache forces every mounted query to refetch with the new session.
    await queryClient.invalidateQueries();
  }, [queryClient]);

  const signOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch (err) {
      // A failed logout call still clears the client: believing you are signed
      // in when you are not is the worse of the two failure modes.
      if (!(err instanceof ApiError)) throw err;
    } finally {
      setUser(null);
      // Drop every cached answer: the next user of this browser must not see
      // the previous one's data.
      queryClient.clear();
    }
  }, [queryClient]);

  const value = useMemo(
    () => ({ user, initialising, signIn, signOut }),
    [user, initialising, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
