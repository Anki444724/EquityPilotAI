"use client";

/**
 * Logged-in account menu: profile summary, Settings, Logout.
 *
 * Uses the existing AuthProvider session (`signOut` → POST /auth/logout)
 * rather than a new identity path. Rendered in the top header so it is
 * reachable on both desktop and mobile without opening the nav drawer.
 */

import { cn } from "@/lib/utils";
import { ChevronDown, LogOut, Settings, User } from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { useAuth } from "./auth-provider";

export interface AccountIdentity {
  name?: string | null;
  email?: string | null;
  role?: string | null;
  tenant_name?: string | null;
  is_dev_identity?: boolean;
}

export function AccountMenu({
  identity,
  compact = false,
}: {
  identity?: AccountIdentity;
  /** Header trigger: hide the name on very small screens. */
  compact?: boolean;
}) {
  const { user: sessionUser, signOut } = useAuth();
  const user: AccountIdentity = identity ?? sessionUser ?? {};
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const initial = (user.name || user.email || "?").slice(0, 1).toUpperCase();

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-label="Account menu"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((value) => !value)}
        className={cn(
          "flex h-10 items-center gap-2 rounded-md border border-[var(--border)] px-2",
          "text-[var(--text-muted)] transition-colors hover:text-[var(--text)]",
          "lg:h-auto lg:py-1.5",
        )}
      >
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-accent-500/15 text-[0.6875rem] font-semibold text-accent-500">
          {initial}
        </span>
        <span
          className={cn(
            "min-w-0 max-w-[9rem] truncate text-left text-xs font-medium text-[var(--text)]",
            compact && "hidden sm:inline",
          )}
        >
          {user.name || user.email || "Account"}
        </span>
        <ChevronDown size={14} className={cn("shrink-0 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label="Account"
          className={cn(
            "absolute right-0 z-40 mt-1 w-64 overflow-hidden rounded-lg border",
            "border-[var(--border-strong)] bg-[var(--bg-elevated)] shadow-xl",
          )}
        >
          <div className="border-b border-[var(--border)] px-3 py-2.5">
            <div className="flex items-start gap-2.5">
              <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full bg-accent-500/15 text-xs font-semibold text-accent-500">
                <User size={14} />
              </span>
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-[var(--text)]">
                  {user.name || "Signed in"}
                </div>
                {user.email && (
                  <div className="truncate text-[0.6875rem] text-[var(--text-muted)]">
                    {user.email}
                  </div>
                )}
                <div className="mt-0.5 truncate text-[0.625rem] uppercase tracking-wide text-[var(--text-muted)]">
                  {[user.role, user.tenant_name].filter(Boolean).join(" · ")}
                </div>
              </div>
            </div>
          </div>

          <Link
            role="menuitem"
            href="/settings"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 px-3 py-2.5 text-sm text-[var(--text)] hover:bg-[var(--bg-subtle)]"
          >
            <Settings size={15} className="text-[var(--text-muted)]" />
            Settings
          </Link>

          <button
            type="button"
            role="menuitem"
            onClick={async () => {
              setOpen(false);
              await signOut();
            }}
            className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm text-loss hover:bg-loss/10"
          >
            <LogOut size={15} />
            Logout
          </button>
        </div>
      )}
    </div>
  );
}
