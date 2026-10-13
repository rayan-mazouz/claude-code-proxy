# Node 24 is the active LTS line; odd-numbered releases are short-lived.
FROM node:24-alpine AS base
ARG PNPM_VERSION=10.34.7
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm install -g "pnpm@${PNPM_VERSION}"
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# No NEXT_PUBLIC_API_BASE_URL is baked in: the dashboard and API share an origin
# (dashboard at /, backend at /api), so the client calls the relative /api path.
RUN pnpm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
# curl is used by the compose healthcheck.
RUN apk add --no-cache curl \
    && addgroup -g 1001 -S nodejs \
    && adduser -S nextjs -u 1001 -G nodejs
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
