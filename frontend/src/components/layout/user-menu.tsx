"use client";

/**
 * The signed-in user block, and the only way out of the application.
 *
 * It replaces the static `UserCard` that sat at the foot of the rail: the
 * shell displayed who you were but offered no way to stop being them, so
 * `AuthProvider.signOut` — which has existed all along — was unreachable from
 * the UI.
 *
 * The menu is a popover rather than three permanent rows because the rail is
 * 224px wide and navigation is what it is for. It renders identically in the
 * desktop rail and the mobile drawer, so there is one implementation to keep
 * correct.
 */

import { useAuth } from "./auth-provider";
import { cn } from "@/lib/utils";
import type { SessionUserFull } from "@/lib/types";
import { ChevronsUpDown, LoaderCircle, LogOut, Settings, UserRound } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

/**
 * The shell's `["me"]` query resolves to `SessionUser`, while AuthProvider
 * holds the wider `SessionUserFull`. The menu needs the four fields both
 * shapes share, so it asks for exactly those and accepts either.
 */
type MenuUser = Pick<SessionUserFull, "name" | "email" | "role" | "is_dev_identity">;

export function UserMenu({
  user,
  onNavigate,
}: {
  user: MenuUser | undefined;
  /** Closes the mobile drawer when an item is chosen. */
  onNavigate?: () => void;
}) {
  const { signOut } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // A popover that survives a click elsewhere is a popover the user has to
  // fight. Escape and outside-clicks both dismiss it.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointer = (event: MouseEvent | TouchEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("touchstart", onPointer);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("touchstart", onPointer);
    };
  }, [open]);

  async function handleSignOut() {
    setSigningOut(true);
    try {
      // `signOut` clears the in-memory access and CSRF tokens, drops every
      // React Query cache entry and forgets the user even when the server
      // call fails — see AuthProvider. Nothing here needs to duplicate that.
      await signOut();
    } finally {
      setSigningOut(false);
      setOpen(false);
      onNavigate?.();
      // The shell renders <SignIn /> as soon as the user is null; replacing
      // the entry means Back cannot return to a page rendered for the
      // previous session.
      router.replace("/");
    }
  }

  const initial = (user?.name ?? user?.email ?? "?").slice(0, 1).toUpperCase();

  return (
    <div ref={containerRef} className="relative border-t border-white/10 p-3">
      {open && (
        <div
          role="menu"
          aria-label="Account"
          className={cn(
            "absolute bottom-full left-3 right-3 z-50 mb-2 overflow-hidden rounded-lg",
            "border border-[var(--border)] bg-[var(--bg-elevated)] shadow-xl",
          )}
        >
          <div className="border-b border-[var(--border)] px-3 py-2.5">
            <div className="truncate text-xs font-medium text-[var(--text)]">
              {user?.name ?? "Signed in"}
            </div>
            {user?.email && (
              <div className="truncate text-[0.6875rem] text-[var(--text-muted)]">
                {user.email}
              </div>
            )}
          </div>

          <Link
            href="/settings"
            role="menuitem"
            onClick={() => { setOpen(false); onNavigate?.(); }}
            className="flex min-h-10 items-center gap-2.5 px-3 py-2 text-[0.8125rem] text-[var(--text)] transition-colors hover:bg-[var(--bg-subtle)]"
          >
            <UserRound size={14} className="shrink-0 text-[var(--text-muted)]" />
            Account
          </Link>
          <Link
            href="/settings#security"
            role="menuitem"
            onClick={() => { setOpen(false); onNavigate?.(); }}
            className="flex min-h-10 items-center gap-2.5 px-3 py-2 text-[0.8125rem] text-[var(--text)] transition-colors hover:bg-[var(--bg-subtle)]"
          >
            <Settings size={14} className="shrink-0 text-[var(--text-muted)]" />
            Settings
          </Link>

          <button
            type="button"
            role="menuitem"
            onClick={handleSignOut}
            disabled={signingOut}
            className="flex min-h-10 w-full items-center gap-2.5 border-t border-[var(--border)] px-3 py-2 text-left text-[0.8125rem] text-loss transition-colors hover:bg-loss/10 disabled:opacity-60"
          >
            {signingOut
              ? <LoaderCircle size={14} className="shrink-0 animate-spin" />
              : <LogOut size={14} className="shrink-0" />}
            {signingOut ? "Signing out…" : "Log out"}
          </button>
        </div>
      )}

      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex w-full min-h-10 items-center gap-2.5 rounded-md px-1 py-1 text-left transition-colors hover:bg-white/10"
      >
        <div className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-white/15 text-[0.6875rem] font-semibold text-white">
          {initial}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs text-white">{user?.name ?? "…"}</div>
          <div className="truncate text-[0.625rem] uppercase tracking-wide text-white/45">
            {user?.role ?? ""}
          </div>
        </div>
        <ChevronsUpDown size={13} className="shrink-0 text-white/45" />
      </button>

      {user?.is_dev_identity && (
        <p className="mt-2 rounded border border-warn/40 bg-warn/10 px-1.5 py-1 text-[0.5625rem] leading-tight text-warn">
          DEV IDENTITY — set NATIVE_AUTH=true for real sign-in
        </p>
      )}
    </div>
  );
}
