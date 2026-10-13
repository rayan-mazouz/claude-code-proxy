# Roadmap

Planned changes that came out of the October 2026 production-readiness review.
Items are ordered by priority within each section. Each entry states the
problem, the intended change, and how to know it is done.

Priorities:

- **P1**: limits scale or safety today; do next.
- **P2**: important hardening; schedule soon.
- **P3**: quality and operability improvements.

Every change must keep existing deployments working: additive schema changes
only, unchanged environment variable names and Compose service/volume names,
and API compatibility with the status line script and the menu-bar client.
Ship live changes through `scripts/blue-green.sh` or `scripts/server-install.sh update`.

---

## Scalability

### P1: Stop blocking the event loop in the proxy path

**Problem.** `proxy_messages` is async, but every database call in it is
synchronous SQLAlchemy, and `events.record_event` opens its own session several
times per request. Each call blocks the worker's event loop, so one slow query
stalls every in-flight stream on that worker. This is the main ceiling on
concurrent users per worker.

**Change.**
1. Move the request-path queries (key lookup, user policy, rate-limit and
   budget checks, account selection) to SQLAlchemy's async engine with
   `psycopg` async, or run each synchronous block with `anyio.to_thread` as an
   intermediate step.
2. Write proxy events through a bounded in-process queue drained by a
   background task in batches, instead of one transaction per event.

**Done when.** A load test with 500 concurrent streaming requests on one worker
shows no event-loop stalls over 50 ms (measure with `loop.slow_callback_duration`
or py-spy), and p99 time-to-first-byte is unchanged from a single request.

### P1: Refresh OAuth tokens without blocking

**Problem.** `rotation.ensure_fresh_token` calls `oauth.refresh_access_token`,
which uses a synchronous `httpx.Client` with a 30-second timeout from inside the
async proxy path. A slow refresh freezes the worker.

**Change.** Add async variants of the OAuth calls (`httpx.AsyncClient`, async
egress client) and use them from the proxy. Keep the row lock that serializes
refreshes. Let the quota refresher pre-refresh tokens that expire within the
leeway, so the request path almost never has to.

**Done when.** No synchronous HTTP call remains reachable from an async route
(add a test that patches `httpx.Client.send` to fail inside the proxy path).

### P2: Dedicated counters for rate limits

**Problem.** Per-user and per-key rate limits count `request.reserved` rows in
`proxy_events` under an advisory lock. It is correct, and indexed since the
review, but every request still inserts a row and counts a window.

**Change.** Keep a small `rate_limit_buckets` table (owner, window start,
count) updated with `INSERT ... ON CONFLICT DO UPDATE`, or sliding-window
counters if Redis is introduced. Keep `request.reserved` events for the audit
trail only if needed.

**Done when.** The rate-limit check is one indexed upsert per window, with
existing limit semantics unchanged (current tests in `test_api.py` pass).

### P2: Retention for events and usage history

**Problem.** Reservation rows are now pruned after two days, but every other
`proxy_events` row and every `usage_records` row is kept forever. Both tables
grow with traffic and slow down the dashboard's aggregate queries.

**Change.** Add `EVENT_RETENTION_DAYS` (default 30) and prune old events in
batches from the quota refresher, as `prune_rate_limit_reservations` does.
Keep usage records, because budgets and spend depend on them, but roll old
rows up into daily per-user/per-model aggregates and point long-range
statistics at the rollups. Consider monthly range partitioning of
`usage_records` once it passes roughly 50 million rows.

**Done when.** Table size is flat under constant traffic, and month-to-date
budget checks plus the Overview page stay under 200 ms on a database holding a
year of history.

### P2: Stop scanning all events in every migration

**Problem.** `sync_canonical_schema` backfills `duration_ms` with a CTE that
aggregates every `request.received` event on each deployment.

**Change.** Record completion of that backfill (a marker row, or skip once no
`usage_records.duration_ms IS NULL` rows remain since a cutoff) so later
deployments skip it.

**Done when.** A deployment against a large database spends under a second in
`app.scripts.migrate` when nothing needs migrating.

### P3: Keyset pagination for events and usage logs

**Problem.** `OFFSET` pagination gets slower the deeper you page and can skip
or repeat rows while new events arrive.

**Change.** Paginate by `(created_at, id)` cursors, and keep `offset` support
for existing clients during a transition.

---

## Security

### P1: Share login throttling across workers

**Problem.** The login throttle keeps its state in each worker process, so with
4 Gunicorn workers the effective limit is up to four times the configured one,
and it resets on restart.

**Change.** Store failure counters in PostgreSQL (`login_failures` table with
an upsert per key and window), or in Redis if it is introduced for rate limits.
Keep the current limits and the `FailureThrottle` interface.

**Done when.** The limit holds exactly across workers (an integration test
running two app instances against one database).

### P1: Make client addresses visible in rootless deployments

**Problem.** In the rootless-Docker install, rootlesskit's port forwarding hides
client IPs, so every request reaches Traefik from one internal address.
Per-address login limits cannot work, and logs show no real client addresses.

**Change.** Either switch the rootless port driver to `slirp4netns` (which keeps
source addresses), or have the installer configure Traefik's
`forwardedHeaders.trustedIPs` for the host's reverse proxy so its
`X-Forwarded-For` is honoured. Document which one the installer uses.

**Done when.** `Dashboard login throttled for source …` logs show real client
addresses on a server installed with `scripts/server-install.sh`.

### P2: Move dashboard sessions to HttpOnly cookies

**Problem.** The admin JWT is stored in `localStorage`, so any script injection
in the dashboard could read it. The CSP added in the review reduces the risk
but still allows inline scripts.

**Change.** Have `/auth/login` set a `Secure; HttpOnly; SameSite=Strict`
session cookie, accept it on admin routes alongside the bearer header during a
transition, add CSRF protection for state-changing requests (double-submit
token or an `Origin` check), and then remove `localStorage` storage.

**Done when.** No JavaScript can read the session, and the existing bearer flow
is retired after one release.

### P2: Nonce-based Content-Security-Policy

**Problem.** `script-src` includes `'unsafe-inline'`, because statically
rendered Next.js pages carry inline bootstrap scripts.

**Change.** Generate a per-request nonce in a Next.js `proxy.ts` (formerly
middleware), render pages dynamically, and switch to
`script-src 'self' 'nonce-…' 'strict-dynamic'`.

**Done when.** The CSP contains no `'unsafe-inline'` for scripts and the
dashboard works in Chromium, Firefox and Safari.

### P3: Restrict the API docs in production

**Problem.** `/api/docs`, `/api/redoc` and `/api/openapi.json` are public. They
reveal no secrets, but they map the admin API for anyone probing the server.

**Change.** Serve them only when `ENV != production`, or put them behind admin
authentication.

### P3: Revoke individual sessions

Sessions are revoked when a password changes or a member is disabled. Add a
per-member session version (an integer column bumped by "Sign out everywhere")
so an administrator can revoke sessions without changing a password.

---

## Deployment and operations

### P1: Zero-downtime updates for the single-server installer

**Problem.** `scripts/server-install.sh update` recreates containers in place,
so requests in flight during an update are cut off and the API is briefly
unavailable.

**Change.** Run the blue-green flow from the installer: two Compose projects
sharing one Traefik on the rootless network, promote after health checks, and
drain the old slot. Reuse `scripts/blue-green.sh` rather than duplicating it.

**Done when.** `scripts/probe-availability.sh` records no failed probes during
an installer update.

### P2: Graceful shutdown for streaming responses

**Problem.** Docker's default 10-second stop timeout and Gunicorn's 30-second
graceful timeout both end long streams during blue-green draining.

**Change.** Set `stop_grace_period` on the backend and pass
`--graceful-timeout` to Gunicorn, sized to the longest expected stream
(for example 300 seconds), and have the blue-green drain wait for the backend's
in-flight request count to reach zero.

### P2: Test the installer on a real host in CI

**Problem.** The installer is checked with ShellCheck and container-based tests
of its functions, but a full `install`, `update` and `uninstall` cycle needs
systemd and rootless Docker, so it is not exercised automatically.

**Change.** Add a scheduled workflow that boots an Ubuntu 24.04 VM (for example
with a self-hosted runner or a cloud VM) and runs install, update with a seeded
database, backup, and uninstall, then checks health and that the data survived.

### P2: Restore command for backups

Add `server-install.sh restore <backup-dir>`. It should stop the app services,
restore the dump into a fresh database inside a transaction, restore `.env`
only when asked, and restart. Today the `backup` command prints the manual
restore steps.

### P3: Metrics and alerting

Expose Prometheus metrics (request rate, latency, upstream status codes, pool
utilization, account cooldowns, database pool waits) on an internal-only port,
and document alerts for pool exhaustion and elevated 5xx rates. Telegram
notifications cover account events only today.

### P3: Calibrate per-account concurrency

`DEFAULT_MAX_CONCURRENT_REQUESTS_PER_ACCOUNT` is a conservative constant (see
the `TODO(concurrency calibration)` in `backend/app/config.py`). Measure the
provider's ceilings per model and subscription tier with controlled parallel
probes, then make the defaults tier-aware.

---

## Frontend

### P2: Frontend tests

**Problem.** The dashboard has no automated tests; changes are verified by lint,
type checking and the production build only.

**Change.** Add component tests (Vitest and Testing Library) for `useResource`,
`useStoredState`, `RangePicker.resolveRange` and the pagination control, and
Playwright smoke tests that sign in and load every page against the Compose
stack in CI.

### P3: Split the largest pages

`users/page.tsx` (about 2,200 lines), `accounts/page.tsx` and the Overview page
mix data loading, tables, cards and several modals. Split them into
per-feature components under `src/components/` so they can be tested and
reviewed independently. Do this together with the frontend tests above.

### P3: Accessibility pass

Check keyboard navigation through the drag-and-drop priority boards and every
custom menu (`SelectMenu`, `EventTypeFilter`, `UserFilter`, `RangePicker`),
verify colour contrast for `fog-500` text on card surfaces, and add an
automated axe check to the Playwright suite.
