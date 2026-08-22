/**
 * Account menu contract: Settings + Logout must stay wired to the existing
 * auth session, not a new identity system.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));

function source(name: string): string {
  return readFileSync(join(here, name), "utf8");
}

describe("account menu", () => {
  it("exposes Settings and Logout and uses the existing signOut session path", () => {
    const menu = source("account-menu.tsx");
    expect(menu).toContain("Account menu");
    expect(menu).toContain("Settings");
    expect(menu).toContain("Logout");
    expect(menu).toContain('href="/settings"');
    expect(menu).toContain("signOut");
    expect(menu).toContain("useAuth");
    expect(menu).not.toMatch(/NEXT_PUBLIC_[A-Z_]*KEY/);
    expect(menu).not.toMatch(/sk-[A-Za-z0-9]/);
  });

  it("is mounted in the app shell header and the sidebar user card", () => {
    const shell = source("app-shell.tsx");
    expect(shell).toContain("AccountMenu");
    expect(shell).toContain('href="/settings"');
    expect(shell).toContain("Logout");
    expect(shell).toContain("signOut");
  });

  it("settings page talks only to existing auth endpoints", () => {
    const page = readFileSync(
      join(here, "../../app/settings/page.tsx"),
      "utf8",
    );
    expect(page).toContain("authApi.me");
    expect(page).toContain("authApi.changePassword");
    expect(page).toContain("authApi.sessions");
    expect(page).toContain("authApi.revokeSessions");
    expect(page).toContain("/api/v1/auth/password");
    expect(page).not.toMatch(/NEXT_PUBLIC_[A-Z_]*KEY/);
  });

  it("logout still goes through authApi.logout and clears the session", () => {
    const provider = source("auth-provider.tsx");
    expect(provider).toContain("authApi.logout");
    expect(provider).toContain("setUser(null)");
    expect(provider).toContain("queryClient.clear()");

    const api = readFileSync(join(here, "../../lib/api.ts"), "utf8");
    expect(api).toContain('"/api/v1/auth/logout"');
    expect(api).toContain("setSession(null)");
  });
});
