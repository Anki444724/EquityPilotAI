import type { NextConfig } from "next";

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
