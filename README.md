# Claude Code Proxy

Claude Code Proxy is a self-hosted Anthropic-compatible gateway for a pool of
authorized Claude subscription accounts. One endpoint gives a team quota-aware
routing, concurrency control, transparent failover, per-user policies, usage
accounting, request archives, Telegram notifications, and an operations
dashboard.

Looking for an OpenAI-compatible Codex proxy instead? See the companion
[Codex Proxy](https://github.com/devasheeshG/codex-proxy).

![Claude Code Proxy overview](docs/screenshots/overview.png)

The screenshots in this README use internally consistent synthetic data. The
overview contains 4.80B tokens (3.84B input + 960M output) across 36,840
requests, so its 80%/20% split is exact; 2.304B cached input tokens produce the
displayed 60% cache hit rate. Five-hour resets are always less than five hours
away and weekly resets are less than seven days away. All names and
`example.test` addresses are fictional; no production credential, prompt, or
database record is used.

> This is an independent project, not an Anthropic product. Add only accounts
> you control and are permitted to use this way, and review the terms that
> apply to your organization and subscription.

## Dashboard throughput history

The full-width **Request analytics** panel defaults to TPS and also includes requests by model and requests by user over time. Every view follows the selected date range, user, and model filters. Ranges up to two days use hourly buckets; longer ranges use daily buckets. Missing TPS measurements appear as gaps. Users contributing less than 5% of requests across the selected range are combined into one Others series in Requests by user. Model dropdowns list only models with activity in the selected range and user scope. **User analytics** opens with Models as its first tab, and **Peak requests** uses the full dashboard width.

Request analytics uses full bucket timestamps and stable per-series identifiers.
Localized hour labels are display-only, so repeated hours across days cannot
select a different tooltip bucket. Tooltip rows read the same series values as
the plotted lines, including zero counts, TPS gaps, and hidden-series choices.

The home page fetches analytics through one read-only `/api/v1/stats/dashboard` response. Independent calculations use a bounded worker pool with separate database sessions; the hidden legacy usage table no longer fetches data. Events combine lookup lists in one request and count paginated parent groups in the grouped selection query. These changes do not alter inference or account routing.

## Shared dashboard behavior

Both proxies use the same **Usage & limits** card: Input rate, Cache hit rate,
Average 5-hour limit, and Average weekly limit. The account-count line remains
plain text: “Quota averages across X / Y authenticated and enabled accounts.”
The standalone Requests by model and Per-request token distribution cards are
removed; requests by model over time remains available in Request analytics.
The dashboard bundle no longer computes the removed distribution panel.
There are no progress bars, pills, remaining percentages, or token-total captions
in this card. The four percentage metrics use a two-by-two layout when the card is narrow.

Weekly-exhausted accounts contribute 100% used to the five-hour average; they
are not removed from its denominator. Reset countdowns show the earliest future
reset that replenishes a nonempty quota window and leaves the account usable:
other exhausted windows and cooldowns must expire by that reset time. Unknown
or later blocking resets are skipped, and simultaneous resets are supported.
These dashboard calculations do not change inference routing or consume resets.

Fresh successful quota probes also release an old quota cooldown when the
recorded quota-failure deadline matches and every observed window is usable.
Short backoffs, unrelated transient failures, missing probe data, and real quota
exhaustion do not clear a cooldown. The Usage & limits card reports the total
banked limit-reset credits for enabled authenticated accounts as a fifth metric column when the provider
supports them (Codex); unsupported providers do not display a fictitious zero.
Narrow cards use two columns with the reset count on a full-width final row.

Both Events pages request server-built, filtered parent groups and request
summaries. Lifecycle events load in chronological order when a request expands
and are cached until the next page refresh. Filters apply to matching requests
inside a parent group, and pagination counts parent groups. Shared regression
tests cover these behaviors in both backends. Both expose paginated, searchable
`GET /api/v1/requests` archive listings; archive availability includes captures
with a response body even when the request body is absent.

The shared frontend analytics and card regression tests run with
`cd frontend && node --test tests/*.test.mjs`. CI and `make verify` run the frontend regression tests in both repositories.
Backend suites run with
`cd backend && uv run pytest -q`. Provider-specific protocol, authentication,
models, thinking modes, caching, installers, and reset-credit features retain
provider-appropriate implementations and tests; OpenAI-only settings are not
exposed as unsupported Claude options.

## Quick start

The supported installation path is agent-assisted: copy the complete prompt in
[INSTALLATION_AGENTS.md](INSTALLATION_AGENTS.md) into your coding or infrastructure
agent. It will ask where to install, whether to use a reverse proxy, and whether
to use an existing Postgres instance before it builds, configures credentials,
creates the first user, installs the client helper, and verifies the deployment.
No manual installation steps are required.

### Single-server install script

For a dedicated Debian or Ubuntu server, `scripts/server-install.sh` installs
Docker, creates an unprivileged `claudeproxy` user running rootless Docker,
generates every secret, and starts the stack behind the bundled Traefik on
plain HTTP (port 18437 by default), ready to sit behind your own TLS proxy or
VPN:

```bash
curl -fsSLO https://raw.githubusercontent.com/rayan-mazouz/claude-code-proxy/main/scripts/server-install.sh
chmod +x server-install.sh
sudo CCP_BIND_ADDRESS=10.8.0.1 ./server-install.sh install   # bind address is optional
sudo ./server-install.sh update     # backs up the database, then pulls and redeploys
sudo ./server-install.sh backup     # on-demand database and .env backup
sudo ./server-install.sh status
```

Re-running `install` on an existing server updates it in place and never
overwrites a value already set in `.env`; it only fills in missing ones. The
model allowlist follows the default list of the deployed revision only while it
is untouched: when `ALLOWED_MODELS` still equals the default the script last
wrote (recorded in `/etc/claude-code-proxy/rootless.conf`), an update replaces
it with the new default so new models arrive with the update. Once you change
it, by hand or on the dashboard [Settings](#settings) page, it is kept. Pass
`CCP_ALLOWED_MODELS=model-a,model-b` to set a list explicitly, or
`CCP_ALLOWED_MODELS=default` to go back to following the default list. The
admin credentials are written to `/root/claude-code-proxy-admin.txt`. Updates
restart containers in place; use the blue-green flow below when you need
zero-downtime rollouts.

The script keeps `.env` owned by `claudeproxy` with mode `0660` and its group
set to the host subgid that the containers' `appuser` (gid 10001) maps to under
rootless Docker (the start of `claudeproxy`'s `/etc/subgid` range + 10000), so
the backend can save Settings changes; every `install` and `update` re-applies
this.

Upgrading from a release that predates the Settings page: download the script
again before running `update` (the copy you saved earlier still sets `.env` to
mode `0600`, which leaves the page read-only), or simply run `update` twice.

### Optional macOS menu-bar app

The native menu-bar app shows live active-account coverage, five-hour and weekly
quota averages, and reset countdowns in a compact popover. Refresh and Settings
are at the top-right. The removed Accounts/demo/activity/widget interfaces are
not part of the minimal app. See [native app documentation](clients/claude-menu-bar/README.md)
for authentication, force-refresh behavior, tests, packaging, and signing limits.

## Project status

This project was built quickly and entirely through AI-assisted development—
plainly, it was vibe-coded. I have not personally reviewed a single line of code
on `main`, so `main` should be treated as working but not human-audited software.
Review it for your own environment and risk model before putting sensitive
accounts or production traffic behind it.

That caveat is about code review, not a lack of real-world use. We have used the
proxy internally for roughly four months. During the last three months it has
handled more than 20 billion tokens and about 70,000 requests for us, and it has
been stable without known operational problems. Our experience is not a
substitute for an independent security review, but this is running software—not
an untested demo.

I intend to review the code as time permits. The `human` branch will contain only
code I have personally reviewed, so it will naturally lag behind `main`: humans
are the bottleneck now (pun intended).

## What it provides

### Reliable pooled routing

- Priority-ordered Claude subscription accounts and independent user priorities.
- PostgreSQL advisory-lock concurrency lanes per account, reserving capacity for
  higher-priority users during bursts. Accounts are not pinned to users.
- Provider five-hour and weekly windows, reset times, configurable thresholds,
  cooldowns, degraded/reauthentication state, and deterministic tie-breaking.
- OAuth refresh with serialized refresh-token rotation and reauthentication
  detection.
- Capacity, overload, 401, 404, 429, quota, and connection failures are handled
  before response bytes reach the client; traffic advances to another account or
  fallback.
- Optional demand-triggered and manual warm-up of cold accounts.
- Claude prompt-cache controls for five-minute (default) or one-hour TTL.

### User and key controls

- Multiple users and multiple labelled API keys per user. Key labels are required
  and plaintext keys are shown only once.
- User priorities and bulk priority assignment, independent of account priority.
- Per-user fallback opt-in (off by default), model allowlists, exact model
  rewrites, thinking levels, request modes, and access policies.
- Limit budgets per user (across all their keys) and per API key: a cap on the
  share of the accounts' 5-hour and weekly limits, in percent of one account's
  window summed across accounts (200% = two full accounts). A capped user or key
  gets HTTP 429 with `Retry-After` until enough of the windows reset. Shares are
  credited when the quota refresher probes an account, so traffic sent between
  two probes can overshoot a budget slightly. Keys also keep per-key request
  rate limits and revocation. (The former token and USD budgets are retired;
  their database columns are kept only so an older release can still start.)
- Spare-capacity users (off by default): they are only routed to accounts with
  quota left after reserving what everyone else is on pace to use in the
  current 5-hour and weekly windows, and get HTTP 429 otherwise. Everyone
  else's usage is measured from the accounts' real usage, so it includes use
  outside the proxy (e.g. claude.ai on the same account).
- The **Users** page shows usage as each user's share of the accounts' current
  5-hour, weekly and monthly windows by default (summed across accounts, so
  100% is one account's full limit), broken down by account or by API key, with
  the limit budget alongside; a toggle switches to cost. Each quota probe splits
  a window's growth by the proxy cost each user and key caused on that account
  in the interval; growth with no proxy traffic is use outside the proxy and is
  not counted.
- Current Max plans have no 5-hour limit: the usage probe reports
  `"five_hour": null`. Such accounts keep no 5-hour state, are never blocked or
  warmed on it, report "No 5-hour limit" in the dashboard, and are left out of
  the 5-hour pool average and the status line's 5h bar. Model-scoped weekly
  allowances the probe reports (for example the Fable share of the weekly limit)
  are stored per account; a request for that model family skips accounts whose
  allowance is at the weekly rotation threshold.
- `THINKING_LEVELS` (default `low,medium,high,max`) lists the effort levels the
  proxy recognizes and the dashboard offers for users and presets. Add `xhigh`
  to make it selectable; a request naming a level outside the list is refused.
- Hashed API keys and Fernet-encrypted OAuth/fallback credentials.

### Team access

- Create, disable, delete, and manage database-backed dashboard members from the
  **Team** page. Each member can be assigned an explicit checked list of exact
  permission strings; there are no required display names or role presets.
- Permissions are split across analytics, archived event bodies, accounts, proxy
  users, API keys, fallbacks, notifications, and team members. Mutating areas
  distinguish read, write, and delete access.
- Authorization is enforced by the API as well as navigation visibility. Unknown
  administrative routes fail closed for non-owner members.
- The environment-configured root login remains the break-glass owner. The
  dashboard deliberately does not store UI audit history or expose session
  revocation controls.

### Deterministic model catalog

Model discovery is local and deterministic, so opening Claude Code does not wait
for every account or fallback provider. The current catalog covers the supported
Sonnet, Haiku, Opus, and Fable families:

| Model | Family |
| --- | --- |
| `claude-fable-5` | Fable |
| `claude-fable-5-1` | Fable |
| `claude-fable-5-2` | Fable |
| `claude-haiku-4-5-20251001` | Haiku |
| `claude-haiku-5-5` | Haiku |
| `claude-opus-4-6` | Opus |
| `claude-opus-4-7` | Opus |
| `claude-opus-4-8` | Opus |
| `claude-opus-5` | Opus |
| `claude-opus-5-5` | Opus |
| `claude-sonnet-5` | Sonnet |
| `claude-sonnet-5-5` | Sonnet |

Exact model rewrites are rendered in events as `target (requested)`.

Supported routes:

| Route | Purpose |
| --- | --- |
| `POST /api/v1/messages` | Anthropic Messages API, streaming and non-streaming |
| `POST /api/v1/messages/count_tokens` | Anthropic token counting |
| `GET /api/v1/models` | Filtered local model catalog |
| `GET /api/v1/me/usage` | Consistent pooled five-hour/weekly usage and reset data |
| `GET /api/health` | Liveness/readiness probe |

Anthropic-compatible API fallbacks are tried only after no eligible subscription
account can serve a request. They have independent priorities, health,
cooldowns, monthly spend caps, encrypted write-only credentials, and per-user
opt-in. Fallback is disabled for every user by default.

## Dashboard tour

### Overview

![Overview dashboard](docs/screenshots/overview.png)

The overview combines pool health, request volume, token and cost totals, and
reusable controls for automatic refresh, manual refresh, local-calendar date
ranges, user selection, and model selection. A pool strip counts usable
accounts, accounts at a limit or cooling down, the weekly headroom left before
each account's threshold, accounts without a 5-hour limit, accounts whose model
allowance is used up, and budgets reached. **Usage & limits** switches between
the pool averages and a per-account table (5-hour, weekly and model-scoped
windows, how much of each came through the proxy, and the account state).
**Limit usage** lists each user's or key's share of the open windows against
their limit budget, with a per-account split. Times use the viewer's local
timezone and a consistent 12-hour format. Token values use compact `K`, `M`, or
`B` notation.

### Accounts

![Accounts dashboard](docs/screenshots/accounts.png)

Accounts appear in priority lanes and a responsive two-column card grid. Cards
retain quota windows, spend, last use, provider checks, authentication state,
cooldown/degraded state, and edit/refresh/warm-up/reauthenticate/disable/delete
actions. Accounts needing reauthentication are ordered first, followed by the
normal usage order. Green action styling indicates an action that is currently
available.

Bulk actions assign a priority to selected accounts without changing their
other settings.

### Users

![Users dashboard](docs/screenshots/users.png)

The Users page manages reusable policy presets. A preset defines allowed
models, global thinking levels and modes, exact model rewrites, and optional
per-model thinking levels/modes. A user may select a preset or **No preset**.
With a preset, edits to individual policy fields—including allowed models and
model rewrites—are explicit overrides; other fields stay inherited. With no
preset, the entire policy is edited directly. Switching to No preset preserves
the current effective policy across restarts. Upgraded installations create a
**Current configuration** preset and assign existing users without changing
their effective policies. User-wide request limits can be set independently
per minute, rolling hour, and rolling 24 hours; they apply across all of the
user's API keys. Zero or blank disables a limit.

Users use the same card language as accounts. Cards show priority, active state,
request mode, thinking levels, allowed models, model rewrites, API-key count,
all-time and current-month tokens/spend, and limit budgets. Drag-and-drop lanes and
bulk assignment make priority changes explicit; saving is atomic.

### Events and request history

![Events dashboard](docs/screenshots/events.png)

Every request is an operational timeline: receipt, account attempt, selection,
capacity or rate-limit result, cooldown, fallback attempt, response, and
exhaustion. The paginated event table filters by date range, user, model, and
event type, color-codes event families, and includes model, thinking level,
account label, status codes, input/output/cache tokens, estimated cost, and
outcome. The default **Individual requests** view shows collapsed request summaries.
Use **Show events / Hide events** to toggle the full lifecycle immediately.
Pagination counts whole requests; filters select matching requests and their
complete histories. **All events** retains the flat event view.

Successful and failed requests are retained. With archiving enabled, an event
can open the exact request and response body from S3-compatible storage;
authorization and cookie headers are never archived.

Request summaries are green for success, red for failure, and neutral while
pending. Stream outcomes are recorded after completion rather than inferred
from HTTP 200 headers. Event records retain a user-name snapshot; deleted users
with older records without a name are labeled **Deleted user**.

#### Event types and routing outcomes

The event log is an operational timeline. One request can produce several
events, while token usage and cost are recorded once per request and attached
to related rows by `request_id`. Do not add the cost shown on multiple event
rows together.

| Event | When it occurs | Status | What happens next |
| --- | --- | ---: | --- |
| `request.received` | An authenticated request enters routing. | — | Account selection begins. |
| `account.attempt` | A pooled subscription account is tried. | — | The request is sent upstream. |
| `account.busy` | The account's concurrency lane is full. | — | The account is skipped and another is tried. |
| `account.capacity` | The provider reports model capacity/unavailability before output. | Upstream | The account is temporarily cooled down and another is tried. |
| `account.cooldown` | The proxy records the cooldown action. | — | The account is kept out of selection temporarily. |
| `account.transient_error` | A retryable pre-output overload/server failure occurs. | Upstream | A short transient cooldown is applied; quota is not marked exhausted. |
| `account.rate_limited` | The provider reports an account/workspace quota limit. | `429` | The provider reset/retry time is used before retrying the account. |
| `account.error` | The request fails before a usable upstream response arrives. | — | The account is excluded for this request and routing continues. |
| `account.response_received` | A pooled account has returned a response candidate. | Candidate | Account failover stops and the response is processed. |
| `fallback.attempt` | A configured pay-as-you-go provider is tried after subscription accounts fail. | — | The fallback request is sent upstream. |
| `fallback.busy` | A fallback provider reaches its concurrency ceiling. | — | Another fallback provider is tried, if available. |
| `fallback.capacity` | A fallback provider reports model capacity/unavailability. | Upstream | The fallback is cooled down and another provider is tried. |
| `fallback.transient_error` | A fallback emits a retryable pre-output error. | Upstream | A short circuit-breaker cooldown is applied. |
| `fallback.error` | A fallback request fails before a response arrives. | — | The provider is marked degraded/cooling down. |
| `fallback.response_received` | A fallback provider has returned a response candidate. | Candidate | Fallback routing stops and the response is processed. |
| `response.returned` | The proxy has a final upstream response to return. | Final HTTP status | Usage is parsed and written to the usage ledger. |
| `request.exhausted` | No eligible subscription account or fallback can serve the request. | `503` | The client receives 503; pool/elevated-503 notifications may be queued. |

Operational rows can show the request's input/output/cache/cost values even
when that particular event did not consume tokens. Those values come from the
single usage record for the same request. A dash means no usage record or no
reported value; zero means a usage record exists and the provider reported no
tokens. Unknown models have no pricing entry and therefore have an estimated
cost of zero.

### API fallbacks

![API fallback settings](docs/screenshots/fallbacks.png)

Fallback entries show label, provider, priority, enabled state, health, spend,
and cap. Operators can enable fallback globally or opt individual users in;
new users remain opted out until explicitly changed. Subscription traffic is
always preferred.

### Notifications

![Notifications settings](docs/screenshots/notifications.png)

Telegram notifications use an encrypted bot token, destination/chat and
optional topic, timezone-aware schedules, per-event enablement, templates, and
repeat cooldowns. A persistent outbox worker keeps Telegram latency out of the
inference path. Account, pool, user/key guardrail, and daily, weekly, and
monthly report events are supported.

### Settings

The owner (the `ADMIN_USERNAME` login) gets a **Settings** page that shows and
edits every known configuration key, grouped by area, with the help text from
`.env.example`. Keys in `.env` that the dashboard does not know are listed
read-only under **Other** (values masked when the name contains `TOKEN`,
`SECRET`, `PASS`, `KEY` or `CREDENTIAL`). Team members cannot be granted it:
it can change the owner password and secrets. Changing a Security, Database or
Egress key, or any secret, also asks for the current owner password. Saving
validates the whole
candidate configuration first (types, ranges, JSON, the secret guards) and
reports problems per field (Compose values such as ports, the bind address,
`DOMAIN`, `ACME_EMAIL` and `ARCHIVE_BORG_HOST_DIR` get the same checks as the
installer's `CCP_*` overrides); it then rewrites the file **in place** under a
lock, keeping comments, blank lines and key order, and appends keys that were
not in the file yet. The new size is reserved before the file is truncated, so
a full disk fails without touching it, and the previous text is copied to
`.env.bak` (mode 0600) when the file's directory is writable (not inside the
containers). Changed key names (never values) are logged.

Each key carries an apply badge:

- **Live** -- read on every use; running processes pick it up within about two
  seconds.
- **Restart** -- captured when a process starts (database pool, log level,
  egress targets and relay credentials, the S3 archive client). The page lists
  saved-but-not-applied keys and offers **Restart backend**, which sends
  `SIGHUP` to the gunicorn master for a graceful rolling restart of the
  workers. The quota refresher, notification dispatcher and archive retention
  sidecars notice the change and exit cleanly, and Docker's
  `restart: unless-stopped` starts them again with the new values.
- **Redeploy** -- consumed by Docker Compose itself (`HTTP_PORT`, `HTTPS_PORT`,
  `HTTP_BIND_ADDRESS`, `DOMAIN` in the Traefik rules, `TRAEFIK_ENTRYPOINT`,
  `ACME_EMAIL`, `ARCHIVE_BORG_HOST_DIR`, the egress relay's ports and hosts).
  These apply on the next `sudo scripts/server-install.sh update` or
  `docker compose up -d`.

The egress relay is a separate Compose project that reads `EGRESS_TARGETS_JSON`,
`EGRESS_RELAY_USERNAME`, `EGRESS_RELAY_TOKEN`, `EGRESS_RELAY_ALLOWED_HOSTS` and
`EGRESS_RELAY_HEALTH_PORT` only when it starts. After changing one, the page
also asks you to run `docker compose -f docker-compose.egress.yml up -d`.

A bad value for a Restart key that the validation cannot catch (an S3 endpoint
or bucket that does not answer, an unreachable egress relay) can make the
restarted backend fail to serve. To recover, fix the value in `.env` with an
editor that writes in place (see below) and restart the backend container
(`docker restart <backend container>`, or `sudo scripts/server-install.sh
update`); the file outranks the container environment, so the fixed value
applies on that restart.

Secrets (`ADMIN_PASSWORD`, `EGRESS_RELAY_TOKEN`, the archive S3 keys) are never
sent to the browser: the page shows *Set* / *Not set* and a field to replace
them; the optional ones (`EGRESS_RELAY_TOKEN`, the archive S3 keys) can also be
cleared. `FERNET_KEY`, `JWT_SECRET` and the database connection
(`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`) are read-only there, because changing them breaks stored
tokens, sessions or the database login, and moving the database is a
redeploy; edit `.env` by hand if you really must. The installer never
generates a new `FERNET_KEY` once the database volume exists.

How it works: Compose bind-mounts `./.env` at `/app/config/.env` in every
backend-image container (read-write only for `backend`) and sets
`CCP_CONFIG_FILE` to it. Values in that file take precedence over the container
environment, which Compose froze from `env_file` when it created the
container; without `CCP_CONFIG_FILE` the repo-root `.env` is used. Because the
file is a single-file bind mount, it must be writable in place by the image's
`appuser` (uid/gid 10001): the single-server script sets this up; elsewhere use
e.g. `sudo chgrp 10001 .env && sudo chmod 0660 .env` with rootful Docker. When
the backend cannot read or write the file the page says so and stays
read-only; a file that cannot be read is never treated as empty (the backend
keeps its current settings, or starts from the container environment).
Values are quoted so python-dotenv and Compose read them identically; values
with line breaks, backslashes or `${` are refused.

Edit `.env` **in place** only: through the dashboard, `server-install.sh`, or
an editor that overwrites the existing file (for example `vim` with
`backupcopy=yes`, or `cat new > .env`). Tools that write a new file and rename
it over the old one (`sed -i`, many editors' "safe save") leave every
container's single-file bind mount pointing at the old file until the next
`update`, and the new file must keep the group and mode the installer set
(group 10001 or its rootless subgid, mode 0660).

## Request lifecycle

```text
client key
   │
   ├─ authenticate user/key and enforce rate limits and limit budgets
   ├─ apply allowlist and exact model rewrite
   ├─ choose highest-priority eligible user lane
   ├─ choose highest-priority account with a free concurrency lane
   ├─ refresh OAuth if needed and forward the Messages request
   ├─ on capacity/429/quota/connection/credential failure: cooldown + next account
   ├─ if the subscription pool is exhausted: optional user-enabled API fallback
   └─ stream response, record usage/cost, emit events, return consistent usage data
```

The proxy does not return an upstream capacity message after a successful retry
on another account. It returns an error only after the subscription and enabled
fallback paths are genuinely exhausted. Usage headers and `/api/v1/me/usage`
share the same pool-level calculation rather than reporting whichever account
answered last.

The default ceiling is **3 concurrent requests per account**. It is deliberately
configurable and should be calibrated for provider, subscription tier, and
model; see the TODO in `backend/app/utils/account_limiter.py` before changing it.

## First-run setup

1. In **Accounts**, start the Claude OAuth flow and complete authorization in
   the displayed browser page.
2. In **Users**, create a user and a labelled API key. The `usr_...` secret is
   shown once; the setup dialog can generate Claude Code settings.
3. Configure Claude Code with the generated command, or set the equivalent
   environment variables:

   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://localhost:8080/api",
       "ANTHROPIC_AUTH_TOKEN": "usr_their_key_here"
     }
   }
   ```

4. Add fallbacks only if needed, and opt each user in explicitly.

The setup helper updates only proxy values in `~/.claude/settings.json`, keeps a
backup, and preserves unrelated Claude Code settings.

### Prompt-cache duration

Claude Code uses a five-minute prompt cache by default. Set
`ENABLE_PROMPT_CACHING_1H=1` in the `env` block for a one-hour cache, or set
`DISABLE_PROMPT_CACHING=1` to disable caching. The included status-line helper
shows the active cache countdown and pooled usage.

## Configuration

Copy `.env.example` for the complete annotated list; once running, the owner
can edit it from the dashboard [Settings](#settings) page. The most important
values are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOMAIN` | `localhost` | Public host and CORS origin |
| `HTTP_PORT` / `HTTPS_PORT` | `8080` / `8443` | Bundled gateway ports |
| `POSTGRES_*` | — | PostgreSQL connection |
| `FERNET_KEY` | required | Encrypts OAuth and fallback credentials |
| `JWT_SECRET` | required | Signs admin sessions (at least 32 characters) |
| `ADMIN_USERNAME` | `admin` | Dashboard username |
| `ADMIN_PASSWORD` | required | Dashboard password |
| `QUOTA_REFRESH_INTERVAL_SECONDS` | `60` | Provider quota refresh interval |
| `MAX_CONCURRENT_REQUESTS_PER_ACCOUNT` | `3` | Per-account concurrency ceiling |
| `DEFAULT_KEY_RATE_LIMIT_PER_MINUTE` | `0` | Default key limit; zero means unlimited |
| `ARCHIVE_ENABLED` | `false` | Store exact request/response bodies in S3-compatible storage |
| `ARCHIVE_REQUIRED` | `true` | Fail closed when a required archive write fails |
| `WARMUP_ENABLED` | `true` | Enable demand-triggered/manual warm-up |
| `ALLOWED_MODELS` | built-in list | Model catalog; the built-in list includes `claude-fable-5-2` |
| `THINKING_LEVELS` | `low,medium,high,max` | Effort levels the proxy accepts and the dashboard offers (add `xhigh` to enable it) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `10` | Database connections per process; keep workers × (size + overflow) under PostgreSQL's `max_connections` |
| `DB_POOL_TIMEOUT_SECONDS` | `30` | How long a request waits for a free database connection |

## Production deployment

Use the repository's blue-green script for live updates. It keeps one shared
Traefik gateway, starts the next Compose slot, waits for health checks, probes
the new API, promotes API before frontend, and drains the old slot only after
both routes identify the new generation.

```bash
scripts/blue-green.sh status
BG_COMPOSE_FILES=docker-compose.yaml:docker-compose.live.yml \
BG_URL=https://claude-proxy.example.com \
make deploy-blue-green
```

For an independent availability log during a rollout:

```bash
scripts/probe-availability.sh https://claude-proxy.example.com/api/health &
probe_pid=$!
make deploy-blue-green
kill "$probe_pid"; wait "$probe_pid" || true
```

Validate rendered Compose labels before changing containers:

```bash
BG_SLOT=blue BG_PRIORITY=1 BG_API_PRIORITY=2 \
docker compose -f docker-compose.yaml -f docker-compose.live.yml config --quiet
```

## Security and data retention

OAuth and fallback secrets are encrypted with Fernet. User keys are stored as
one-way hashes. Archived bodies are optional, compressed, and written to an
S3-compatible bucket; prompt and tool content may contain sensitive data, so
restrict bucket access and configure retention. Authorization and cookie
headers are excluded from captures. Review `SECURITY.md` before exposing the
dashboard publicly.

Dashboard sessions are bound to the credential they were issued for: changing
`ADMIN_PASSWORD` or a member's password signs out every existing session.
Failed sign-ins are throttled per account and per client address (when the
gateway can see real client addresses). Team members can only grant, or
manage members holding, permissions they have themselves.

## Development

```bash
make verify
cd backend && uv sync && uv run pytest
cd frontend && pnpm install && pnpm lint && pnpm build
```

The main directories are `backend/` (FastAPI, SQLAlchemy, Alembic), `frontend/`
(Next.js dashboard), `clients/` (Claude Code helpers), and the Compose/deployment
files at the repository root.

## Roadmap

Planned work, with priorities and completion criteria, is tracked in
[docs/ROADMAP.md](docs/ROADMAP.md).

## Contributing and project policy

Issues and pull requests are genuinely welcome. Feel free to open an issue for
anything that could be clearer or better, or send a PR if you want to fix or
improve something yourself.

- Use Issues for reproducible bugs, focused feature requests, and documentation
  problems. Search first, use one issue per concern, reproduce against the latest
  `main`, and include sanitized logs when relevant. There is no support SLA.
- Never disclose credentials, archived prompts, private logs, or database data in
  an issue. Report vulnerabilities through the repository's
  [private vulnerability-reporting flow](https://github.com/devasheeshG/claude-code-proxy/security/advisories/new).
- Small fixes and documentation PRs do not require a prior issue. Discuss large
  features, migrations, protocol changes, and architectural work in an issue
  before implementation.
- PRs branch from `main`, stay focused, include relevant tests and UI screenshots,
  update documentation, and pass backend/frontend CI. A merge into `main` does
  not mean the code received line-by-line human review; use `human` when that
  distinction matters.
- Draft PRs are welcome. There is currently no CLA; contributions are made under
  AGPL-3.0 and participation follows the Code of Conduct.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and the
[previous detailed README](README.old.md) for the complete policy and upgrade
context.

## License

[AGPL-3.0](LICENSE). If you run a modified version as a network service, make
the corresponding source available to its users.

## Per-account egress IPs

The proxy supports several approved public egress IPs from one EC2 instance.
Each secondary private address is attached to the existing ENI and mapped to
an Elastic IP. The boot helper in `ops/aws-egress/` restores those addresses;
`docker-compose.egress.yml` runs an authenticated host-network CONNECT relay
per source address. Backend containers use `host.docker.internal`, so no
second gateway or public relay port is required.

Configure `EGRESS_TARGETS_JSON` in the protected `.env`. In Accounts → Edit,
the first enabled target in configuration order is the default (and all
existing accounts are initialized to it). Selecting another target pins that
Claude subscription account for inference, OAuth, quota refresh, and warm-up;
there is no automatic rotation or cross-target failover. The UI shows private
and public address metadata only. Keep the relay token secret and allowlist
only Anthropic hosts (`api.anthropic.com`/`anthropic.com`). Verify the relay
project, health endpoint, and a sanitized Anthropic request before serving
traffic; deploy app changes through blue-green and keep one Traefik.


### Pool exhaustion and reset credits

Weekly-exhausted Claude accounts yield to remaining available pooled accounts.
The Claude provider integration has no banked reset-credit redemption API, so
this proxy never invents or automatically claims a reset. Requests retain
normal failover and bounded pool waiting; provider quotas recover through
natural resets. The paired Codex proxy uses reset credits only as a last
resort after model-compatible account capacity is exhausted.

Presets include **Allow API fallback providers**. Users inherit this setting unless they select a per-user fallback override. Clearing the override restores preset inheritance. The migration preserves existing user fallback permissions.

Request metrics are finalized when provider usage arrives. Interrupted or incomplete responses without usage are shown as **Unavailable**, rather than zero-token, zero-cost successes. HTTP 200 alone does not mean generation finished; historical captures without final usage cannot supply exact token counts or TPS.

## Unified capacity alerts

**Request capacity exhausted** replaces Entire account pool unavailable and
503 capacity exhaustion. An exhausted HTTP 503 request queues one alert type,
including model-specific failures, with the model, failure reason, pool counts,
next useful capacity reset, and a rolling 60-second failure/user summary.

Deployment automatically merges old database rules per channel: enabled when
either old rule was enabled; the longest enabled-rule cooldown wins; customized
old templates are preserved together using the supported legacy variables.
Existing merged-rule settings are never overwritten on subsequent migrations.
Legacy queued/retrying duplicates are suppressed; sent history stays intact and
still contributes to the shared notification cooldown. No migration sends a
notification. A zero cooldown allows one alert per exhausted request.
