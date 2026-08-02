import type { Metadata, Viewport } from "next";
import { Providers } from "@/components/layout/providers";
import "./globals.css";

/**
 * Next emits `width=device-width, initial-scale=1` by default, which is
 * correct but does not cover two things this platform needs.
 *
 * `viewportFit: "cover"` lets the page paint into the display cutout area on
 * notched iPhones; the shell then re-inserts the safe-area insets as padding,
 * so the header background reaches the top of the screen while its contents
 * stay clear of the notch.
 *
 * `maximumScale` is deliberately NOT set. Locking zoom is the usual reflex
 * once a layout is responsive, and it is an accessibility regression: a user
 * who needs to magnify a figure in a balance sheet must be allowed to.
 * iOS 10+ ignores the restriction anyway, so setting it only harms Android.
 */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export const metadata: Metadata = {
  title: "Institutional Equity Research Platform",
  description:
    "Bloomberg-grade equity research: canonical financials, forecasting, DCF valuation, institutional scoring and AI analysis.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body className="antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
