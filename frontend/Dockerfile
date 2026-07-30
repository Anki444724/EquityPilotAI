# =============================================================================
# Frontend image — multi-stage Next.js build.
#
# Three stages: dependencies, build, runtime. The runtime stage carries only
# Next's standalone output, which is the traced set of files the server
# actually needs — typically a tenth of `node_modules`.
# =============================================================================

# --- stage 1: dependencies ---------------------------------------------------
FROM node:20-slim AS deps

WORKDIR /app
COPY package.json package-lock.json* ./
# `npm ci` rather than `npm install`: reproducible, and it fails loudly when
# the lockfile and manifest disagree instead of quietly resolving something
# else.
RUN npm ci


# --- stage 2: build ----------------------------------------------------------
FROM node:20-slim AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* values are inlined at build time, so the API URL must be known
# here rather than at run time. A deployment pointing at a different backend
# needs a rebuild — the cost of static generation.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build


# --- stage 3: runtime --------------------------------------------------------
FROM node:20-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

RUN groupadd --system --gid 1001 nodejs \
    && useradd --system --uid 1001 --gid nodejs nextjs

WORKDIR /app

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
