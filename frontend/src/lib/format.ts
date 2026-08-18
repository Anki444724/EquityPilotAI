/**
 * Number formatting for institutional financial display.
 *
 * Rules inherited from the workbook's display discipline:
 *  1. Never render a fabricated number — null/undefined shows as an em dash.
 *  2. Always carry units (₹ cr, %, x).
 *  3. Negatives in parentheses, Indian convention.
 */

export const EM_DASH = "—";

/** Indian digit grouping: 1,23,45,678 */
export function indianGroup(value: number, decimals = 1): string {
  const neg = value < 0;
  const fixed = Math.abs(value).toFixed(decimals);
  const [intPart, decPart] = fixed.split(".");
  let out: string;
  if (intPart.length <= 3) {
    out = intPart;
  } else {
    const last3 = intPart.slice(-3);
    const rest = intPart.slice(0, -3);
    out = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3;
  }
  const joined = decPart ? `${out}.${decPart}` : out;
  return neg ? `(${joined})` : joined;
}

/** ₹ crore figure. */
export function crore(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  return indianGroup(value, decimals);
}

/** Compact market cap: ₹1.23 L Cr / ₹4,567 Cr */
export function marketCap(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  if (Math.abs(value) >= 100000) return `₹${(value / 100000).toFixed(2)} L Cr`;
  return `₹${indianGroup(value, 0)} Cr`;
}

/** Ratio expressed as a percentage. `0.1552 -> 15.5%` */
export function percent(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  const v = value * 100;
  const s = `${Math.abs(v).toFixed(decimals)}%`;
  return v < 0 ? `(${s})` : s;
}

/** Already-percentage value. `15.5 -> 15.5%` */
export function percentPoints(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  return `${value.toFixed(decimals)}%`;
}

/** Multiple, e.g. net debt / EBITDA. */
export function multiple(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  return `${value.toFixed(decimals)}x`;
}

/** Share price in rupees. */
export function rupees(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  return `₹${indianGroup(value, decimals)}`;
}

export function plainNumber(value: number | null | undefined, decimals = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  return indianGroup(value, decimals);
}

/** Tailwind colour class for a signed value. */
export function signClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "text-[var(--text-muted)]";
  if (value > 0) return "text-[var(--color-gain)]";
  if (value < 0) return "text-[var(--color-loss)]";
  return "text-[var(--text-muted)]";
}

/** Fiscal-year label: 2025 -> FY25 */
export function fiscalYear(year: number): string {
  return `FY${String(year).slice(-2)}`;
}

/**
 * The price a surface should DISPLAY: the live market figure when present,
 * else the stored fallback. `market.live_price` comes from the shared
 * LiveMarketService; `current_price` is the historical DB column and is only
 * ever a fallback here, never presented as a live quote.
 */
export function marketPrice(c: {
  market?: { live_price: number | null } | null;
  current_price?: number | null;
} | null | undefined): number | null {
  const live = c?.market?.live_price;
  if (live !== null && live !== undefined && !Number.isNaN(live)) return live;
  const stored = c?.current_price;
  if (stored === null || stored === undefined || Number.isNaN(stored)) return null;
  return stored;
}

export function isLivePrice(source: string | null | undefined): boolean {
  return Boolean(source && !source.includes("Internal") && !source.includes("Uploaded") && source !== "Unavailable");
}

export function priceSourceLabel(source: string | null | undefined): string {
  if (!source || source === "Unavailable") return "Price unavailable";
  if (source.includes("Yahoo")) return "Yahoo Finance";
  if (source.includes("Internal")) return "Stored company data";
  if (source.includes("Uploaded")) return "Company filing";
  return source;
}

export function lastUpdated(value: string | null | undefined): string {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not available";
  return date.toLocaleString(undefined, {
    dateStyle: "medium", timeStyle: "short",
  });
}
