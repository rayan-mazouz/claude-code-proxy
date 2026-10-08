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

## Quick start

The supported installation path is agent-assisted: copy the complete prompt in
[INSTALLATION_AGENTS.md](INSTALLATION_AGENTS.md) into your coding or infrastructure
agent. It will ask where to install, whether to use a reverse proxy, and whether
to use an existing Postgres instance before it builds, configures credentials,
creates the first user, installs the client helper, and verifies the deployment.
No manual installation steps are required.

### Optional macOS menu-bar app

If you use the dashboard from a Mac, the companion menu-bar app provides compact
Overview and Accounts views, including the same `All`, `Authenticated`, and
`Usable` account filters. The latest ad-hoc-signed build is published at the
[Claude Code Proxy macOS releases](https://github.com/devasheeshG/claude-code-proxy/releases)
page. Download `Claude-Code-Proxy-macOS.dmg`, open the disk image, and move
**Claude Code Proxy.app** to Applications. This build is intended for local use;
macOS may require Control-clicking the app and choosing **Open** because it is
not notarized.

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
- Per-user monthly/lifetime token and spend budgets, plus per-key request/token
  limits and revocation.
- Spare-capacity users (off by default): they are only routed to accounts with
  quota left after reserving what everyone else is on pace to use in the
  current 5-hour and weekly windows, and get HTTP 429 otherwise. Everyone
  else's usage is measured from the accounts' real usage, so it includes use
  outside the proxy (e.g. claude.ai on the same account).
- The **Users** page shows usage as cost or as each user's share of the
  accounts' current 5-hour, weekly and monthly windows (summed across accounts,
  so 100% is one account's full limit). Each quota probe splits a window's
  growth by the proxy cost each user caused on that account in the interval;
  growth with no proxy traffic is use outside the proxy and is not counted.
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
| `claude-haiku-4-5-20251001` | Haiku |
| `claude-opus-4-6` | Opus |
| `claude-opus-4-8` | Opus |
| `claude-opus-5` | Opus |
| `claude-opus-5-5` | Opus |
| `claude-sonnet-5` | Sonnet |

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
ranges, user selection, and model selection. Times use the viewer's local
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
all-time and current-month tokens/spend, and budgets. Drag-and-drop lanes and
bulk assignment make priority changes explicit; saving is atomic.

### Events and request history

![Events dashboard](docs/screenshots/events.png)

Every request is an operational timeline: receipt, account attempt, selection,
capacity or rate-limit result, cooldown, fallback attempt, response, and
exhaustion. The paginated event table filters by date range, user, model, and
event type, color-codes event families, and includes model, thinking level,
account label, status codes, input/output/cache tokens, estimated cost, and
outcome. Request IDs are intentionally omitted from the normal table.

Successful and failed requests are retained. With archiving enabled, an event
can open the exact request and response body from S3-compatible storage;
authorization and cookie headers are never archived.

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

## Request lifecycle

```text
client key
   │
   ├─ authenticate user/key and enforce rate/token/spend budgets
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

Copy `.env.example` for the complete annotated list. The most important values
are:

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

## Development

```bash
make verify
cd backend && uv sync && uv run pytest
cd frontend && pnpm install && pnpm lint && pnpm build
```

The main directories are `backend/` (FastAPI, SQLAlchemy, Alembic), `frontend/`
(Next.js dashboard), `clients/` (Claude Code helpers), and the Compose/deployment
files at the repository root.

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

### Context-window policy

Presets and users include an **Allow extended context window** policy switch. It is disabled by default and available as a per-user override. The migration explicitly enables it for the existing user `Devasheesh`; provider-specific context capabilities remain negotiated by the upstream endpoint.

### Pool exhaustion and reset credits

Weekly-exhausted Claude accounts yield to remaining available pooled accounts.
The Claude provider integration has no banked reset-credit redemption API, so
this proxy never invents or automatically claims a reset. Requests retain
normal failover and bounded pool waiting; provider quotas recover through
natural resets. The paired Codex proxy uses reset credits only as a last
resort after model-compatible account capacity is exhausted.

Presets include **Allow API fallback providers**. Users inherit this setting unless they select a per-user fallback override. Clearing the override restores preset inheritance. The migration preserves existing user fallback permissions.
