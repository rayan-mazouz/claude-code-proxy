import type { NextConfig } from "next";

// The console only talks to its own origin (the API is served under /api), so
// everything is locked to 'self'. Next.js injects inline bootstrap scripts into
// statically rendered pages, which is why script-src keeps 'unsafe-inline';
// external script sources are still refused.
const contentSecurityPolicy = [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
    { key: "Content-Security-Policy", value: contentSecurityPolicy },
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "X-Frame-Options", value: "DENY" },
    { key: "Referrer-Policy", value: "no-referrer" },
    { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
    { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

const nextConfig: NextConfig = {
    output: "standalone",
    poweredByHeader: false,
    async headers() {
        return [{ source: "/:path*", headers: securityHeaders }];
    },
    // Local-dev convenience only: when API_PROXY_TARGET is set (e.g. running
    // `next dev` outside the compose stack), proxy same-origin "/api" calls to a
    // backend so the dashboard works without the gateway. In production the var
    // is unset and the Traefik gateway handles "/api" routing, so this adds no
    // rewrite and changes nothing.
    async rewrites() {
        const target = process.env.API_PROXY_TARGET;
        if (!target) return [];
        return [{ source: "/api/:path*", destination: `${target}/api/:path*` }];
    },
};

export default nextConfig;
