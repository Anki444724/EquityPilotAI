"use client";

/**
 * Deep-link sub-routes for the Enterprise Admin Panel.
 *
 * The admin console is a single tabbed page at `/admin`. These routes let a
 * URL like `/admin/companies` or `/admin/live-market` open the console
 * directly on the matching tab instead of requiring a click.
 */
import { use } from "react";
import AdminConsole from "@/components/admin/admin-console";

//: URL slug -> internal tab key. Add a mapping here for any new sidebar tab.
const SLUG_TO_TAB: Record<string, string> = {
  "": "overview",
  overview: "overview",
  members: "members",
  billing: "billing",
  usage: "usage",
  keys: "keys",
  audit: "audit",
  activity: "activity",
  "recycle-bin": "recycle-bin",
  companies: "companies",
  financials: "financials",
  "live-market": "market",
  market: "market",
  ai: "ai-score",
  "ai-score": "ai-score",
  documents: "documents",
  users: "users",
};

export default function AdminSectionPage({
  params,
}: {
  params: Promise<{ section: string }>;
}) {
  const { section } = use(params);
  const tab = SLUG_TO_TAB[section] ?? "overview";
  return <AdminConsole initialTab={tab} />;
}
