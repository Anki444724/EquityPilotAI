import type { NextConfig } from "next";

/**
 * Where the Next server proxies `/api` and `/health`.
 *
 * In Docker Compose this is `http://api:8000` (the backend service name).
 * Local `next dev` falls back to localhost. Evaluated at build time for the
 * standalone image, so it must be passed as a build arg, not only a runtime
 * env var.
 */
function internalApiOrigin(): string {
  const explicit = process.env.API_INTERNAL_URL?.replace(/\/$/, "");
  if (explicit) return explicit;
  const pub = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "";
  if (pub && !/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/i.test(pub)) {
    return pub;
  }
  return "http://localhost:8000";
}

const INTERNAL_API = internalApiOrigin();

const nextConfig: NextConfig = {
  /**
   * Emit a self-contained server bundle.
   *
   * Next traces the modules the server actually imports and copies just those
   * into `.next/standalone`. The production image then carries roughly a tenth
   * of `node_modules`, and — more usefully — nothing the application does not
   * import, so a vulnerability in an unused transitive dependency is not
   * shipped at all.
   */
  output: "standalone",

  /**
   * Security headers, mirroring the ones the API sets on its own responses.
   *
   * A frontend served without these is where a clickjacking or MIME-sniffing
   * attack lands, and the browser only honours what the origin serving the
   * document sends — the API's headers do not cover these pages.
   */
  /**
   * Same-origin API. The browser calls `/api/v1/...` on equitypilot.in;
   * this process forwards to the backend container. Cookies stay first-party.
   */
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${INTERNAL_API}/api/:path*` },
      { source: "/health", destination: `${INTERNAL_API}/health` },
      { source: "/health/:path*", destination: `${INTERNAL_API}/health/:path*` },
      { source: "/docs", destination: `${INTERNAL_API}/docs` },
      { source: "/openapi.json", destination: `${INTERNAL_API}/openapi.json` },
    ];
  },

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "geolocation=(), microphone=(), camera=(), payment=()",
          },
        ],
      },
    ];
  },

};

export default nextConfig;
