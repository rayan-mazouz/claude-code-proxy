# Path: app/utils/config_registry.py
# Description: Metadata for every key the dashboard Settings page can show or edit in the .env config file.

"""Registry of configuration keys.

``apply`` says when a change takes effect:

- ``live``: read on every use; running processes pick the new value up within a few seconds.
- ``restart``: captured once at process start (DB pool, logging, egress pool, archive client); the backend needs a
  rolling restart, and the sidecars exit on their own so Docker restarts them with the new value.
- ``redeploy``: consumed by Docker Compose itself (ports, Traefik labels, bind mounts); applied on the next
  ``scripts/server-install.sh update`` or ``docker compose up -d``.

Secret values are never returned by the API. ``read_only`` keys can only be changed by hand, because changing them
breaks existing data (the Fernet key, the database connection) or every session at once. ``requires_password`` keys
(security, database and egress settings, and every secret) can only be changed by re-entering the owner password.
``relay_restart`` keys are also read by the separate egress relay project, which only reads them when it starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

SettingType = Literal["string", "int", "float", "bool", "json", "list", "choice", "secret"]
ApplyClass = Literal["live", "restart", "redeploy"]


@dataclass(frozen=True)
class SettingSpec:
    key: str
    group: str
    label: str
    help: str
    type: SettingType
    apply: ApplyClass
    choices: tuple[str, ...] = ()
    read_only: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # Default shown for keys Compose consumes (Settings fields take their default from the model).
    default: Optional[str] = None
    # JSON keys: the top-level shape the value must have.
    json_shape: Optional[Literal["object", "array"]] = None
    # Optional secrets the dashboard may clear (write an empty value).
    clearable: bool = False
    # Also read by the egress relay (docker-compose.egress.yml) at its start.
    relay_restart: bool = False

    @property
    def secret(self) -> bool:
        return self.type == "secret"

    @property
    def requires_password(self) -> bool:
        """Changing this key needs the current owner password (it can lock the owner out or redirect traffic)."""
        return self.secret or self.group in PASSWORD_GROUPS or self.key in PASSWORD_KEYS


GROUPS: tuple[str, ...] = (
    "General",
    "Domain & gateway",
    "Security",
    "Database",
    "Models",
    "Rate limits & concurrency",
    "Quota & warm-up",
    "Spare capacity",
    "Fallbacks",
    "Egress",
    "Archive",
    "Archive retention",
)

_G, _D, _S, _DB, _M, _R, _Q, _SP, _F, _E, _A, _AR = GROUPS

# Group for keys found in the file that the registry does not know (shown read-only).
OTHER_GROUP = "Other"
PASSWORD_GROUPS: frozenset[str] = frozenset({_S, _DB, _E})
PASSWORD_KEYS: frozenset[str] = frozenset({"JWT_EXPIRE_MINUTES", "ADMIN_USERNAME", "ADMIN_PASSWORD"})
RELAY_RESTART_COMMAND = "docker compose -f docker-compose.egress.yml up -d"
_RELAY_NOTE = f" The egress relay only reads this when it starts: after a change also run `{RELAY_RESTART_COMMAND}`."

SPECS: tuple[SettingSpec, ...] = (
    # --- General ---
    SettingSpec("ENV", _G, "Environment", "Deployment label (for example production). Informational only.", "string", "live"),
    SettingSpec(
        "LOG_LEVEL",
        _G,
        "Log level",
        "Log verbosity. INFO is the production default; use DEBUG only while diagnosing.",
        "choice",
        "restart",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
    # --- Domain & gateway (Compose) ---
    SettingSpec(
        "DOMAIN",
        _D,
        "Public domain",
        "Host the proxy is served on (dashboard at https://DOMAIN/, API at https://DOMAIN/api). Also the CORS origin "
        "and the Traefik routing rule, so it applies on the next redeploy.",
        "string",
        "redeploy",
    ),
    SettingSpec(
        "HTTP_PORT",
        _D,
        "HTTP port",
        "Host port the bundled Traefik publishes for plain HTTP.",
        "int",
        "redeploy",
        minimum=1,
        maximum=65535,
        default="8080",
    ),
    SettingSpec(
        "HTTPS_PORT",
        _D,
        "HTTPS port",
        "Host port the bundled Traefik publishes for HTTPS.",
        "int",
        "redeploy",
        minimum=1,
        maximum=65535,
        default="8443",
    ),
    SettingSpec(
        "HTTP_BIND_ADDRESS",
        _D,
        "Bind address",
        "Host IPv4 address the gateway binds (single-server install), e.g. a VPN interface IP. 0.0.0.0 binds every interface.",
        "string",
        "redeploy",
        default="0.0.0.0",
    ),
    SettingSpec(
        "TRAEFIK_ENTRYPOINT",
        _D,
        "Traefik entrypoint",
        "web serves plain HTTP; websecure switches the routers to TLS with Let's Encrypt.",
        "choice",
        "redeploy",
        choices=("web", "websecure"),
        default="web",
    ),
    SettingSpec("ACME_EMAIL", _D, "ACME email", "Contact address Let's Encrypt registers for the certificate.", "string", "redeploy"),
    # --- Security ---
    SettingSpec("ADMIN_USERNAME", _S, "Owner username", "Username of the break-glass dashboard owner login.", "string", "live"),
    SettingSpec(
        "ADMIN_PASSWORD",
        _S,
        "Owner password",
        "Owner login password (at least 8 characters). Changing it signs out every owner session, including this one.",
        "secret",
        "live",
    ),
    SettingSpec(
        "JWT_SECRET",
        _S,
        "JWT secret",
        "Signs dashboard sessions. Read-only here: changing it signs everyone out. Edit .env by hand if you must rotate it.",
        "secret",
        "live",
        read_only=True,
    ),
    SettingSpec(
        "JWT_ALGORITHM",
        _S,
        "JWT algorithm",
        "HMAC algorithm for session tokens. Changing it signs everyone out.",
        "choice",
        "live",
        choices=("HS256", "HS384", "HS512"),
    ),
    SettingSpec(
        "JWT_EXPIRE_MINUTES",
        _S,
        "Session lifetime (minutes)",
        "How long a dashboard session stays valid.",
        "int",
        "live",
        minimum=1,
        maximum=60 * 24 * 90,
    ),
    SettingSpec(
        "FERNET_KEY",
        _S,
        "Fernet key",
        "Encrypts pooled-account OAuth tokens at rest. Read-only: a new key makes every stored token unreadable.",
        "secret",
        "restart",
        read_only=True,
    ),
    # --- Database ---
    SettingSpec(
        "POSTGRES_HOST",
        _DB,
        "Host",
        "PostgreSQL host (the bundled database's service name is postgres). Read-only: moving the database is a redeploy, not a dashboard change.",
        "string",
        "restart",
        read_only=True,
    ),
    SettingSpec(
        "POSTGRES_PORT",
        _DB,
        "Port",
        "PostgreSQL port. Read-only: moving the database is a redeploy, not a dashboard change.",
        "int",
        "restart",
        minimum=1,
        maximum=65535,
        read_only=True,
    ),
    SettingSpec(
        "POSTGRES_DB",
        _DB,
        "Database",
        "Database name. Read-only: the bundled PostgreSQL only creates it when its volume is first initialised.",
        "string",
        "restart",
        read_only=True,
    ),
    SettingSpec(
        "POSTGRES_USER",
        _DB,
        "User",
        "Database role. Read-only: the bundled PostgreSQL only reads it on first initialisation.",
        "secret",
        "restart",
        read_only=True,
    ),
    SettingSpec(
        "POSTGRES_PASSWORD",
        _DB,
        "Password",
        "Database password. Read-only: the bundled PostgreSQL only reads it on first initialisation.",
        "secret",
        "restart",
        read_only=True,
    ),
    SettingSpec(
        "DB_POOL_SIZE",
        _DB,
        "Pool size",
        "Connections kept open per process. Keep processes x (size + overflow) below PostgreSQL's max_connections (100 by default).",
        "int",
        "restart",
        minimum=1,
        maximum=100,
    ),
    SettingSpec("DB_MAX_OVERFLOW", _DB, "Max overflow", "Extra connections per process opened under load.", "int", "restart", minimum=0, maximum=200),
    SettingSpec(
        "DB_POOL_TIMEOUT_SECONDS",
        _DB,
        "Pool timeout (seconds)",
        "How long a request waits for a free connection before failing.",
        "float",
        "restart",
        minimum=0.001,
        maximum=600,
    ),
    # --- Models ---
    SettingSpec(
        "ALLOWED_MODELS",
        _M,
        "Allowed models",
        "Comma-separated model IDs exposed by the local catalog and dashboard. Blank keeps the built-in lineup.",
        "list",
        "live",
    ),
    SettingSpec(
        "THINKING_LEVELS",
        _M,
        "Thinking levels",
        "Comma-separated effort levels the proxy recognizes and the dashboard offers, lowest first. Add xhigh to make it selectable.",
        "list",
        "live",
        choices=("low", "medium", "high", "xhigh", "max"),
    ),
    # --- Rate limits & concurrency ---
    SettingSpec(
        "DEFAULT_KEY_RATE_LIMIT_PER_MINUTE",
        _R,
        "Default key rate limit (req/min)",
        "Proxy rate limit for API keys with no explicit override; 0 = unlimited.",
        "int",
        "live",
        minimum=0,
    ),
    SettingSpec(
        "MAX_CONCURRENT_REQUESTS_PER_ACCOUNT",
        _R,
        "Concurrent requests per account",
        "Default in-flight request ceiling per pooled account (1-32).",
        "int",
        "live",
        minimum=1,
        maximum=32,
    ),
    SettingSpec(
        "MODEL_CONCURRENCY_LIMITS_JSON",
        _R,
        "Per-model concurrency",
        'JSON object mapping a model ID to its per-account concurrency, e.g. {"claude-opus-5-5": 2}.',
        "json",
        "live",
        json_shape="object",
    ),
    SettingSpec(
        "TIER_CONCURRENCY_LIMITS_JSON",
        _R,
        "Per-tier concurrency",
        'JSON object mapping a subscription tier to its per-account concurrency, e.g. {"max": 4}.',
        "json",
        "live",
        json_shape="object",
    ),
    SettingSpec(
        "POOL_WAIT_TIMEOUT_SECONDS",
        _R,
        "Pool wait timeout (seconds)",
        "How long a request waits for a cooling or refreshing account before returning 503.",
        "int",
        "live",
        minimum=0,
    ),
    SettingSpec(
        "POOL_WAIT_POLL_INTERVAL_SECONDS",
        _R,
        "Pool wait poll interval (seconds)",
        "How often a waiting request re-checks the pool.",
        "float",
        "live",
        minimum=0.1,
    ),
    SettingSpec(
        "TRANSIENT_UPSTREAM_COOLDOWN_SECONDS",
        _R,
        "Transient upstream cooldown (seconds)",
        "Short rest applied to an account after a transient upstream error.",
        "int",
        "live",
        minimum=0,
    ),
    # --- Quota & warm-up ---
    SettingSpec(
        "QUOTA_REFRESH_INTERVAL_SECONDS",
        _Q,
        "Quota refresh interval (seconds)",
        "How often the quota-refresher sidecar re-probes each account's usage and subscription tier.",
        "int",
        "live",
        minimum=5,
    ),
    SettingSpec(
        "WARMUP_ENABLED",
        _Q,
        "Warm-up",
        "Demand-triggered account-window warm-up. No synthetic traffic is sent while idle.",
        "bool",
        "live",
    ),
    SettingSpec(
        "WARMUP_TRIGGER_POOL_USAGE_PCT",
        _Q,
        "Warm-up trigger (pool usage 0-1)",
        "Aggregate five-hour pool usage at which all other eligible cold accounts are warmed in one batch.",
        "float",
        "live",
        minimum=0,
        maximum=1,
    ),
    SettingSpec(
        "WARMUP_WEEKLY_RESERVE_PCT",
        _Q,
        "Warm-up weekly reserve (0-1)",
        "Accounts at or above this weekly usage are never warmed.",
        "float",
        "live",
        minimum=0,
        maximum=1,
    ),
    SettingSpec("WARMUP_MODEL", _Q, "Warm-up model", "Model used for warm-up requests.", "string", "live"),
    # --- Spare capacity ---
    SettingSpec(
        "SPARE_CAPACITY_FIVE_HOUR_FLOOR",
        _SP,
        "Five-hour floor (0-1)",
        "Share of each account's 5-hour window always held back from spare-capacity users.",
        "float",
        "live",
        minimum=0,
        maximum=1,
    ),
    SettingSpec(
        "SPARE_CAPACITY_WEEKLY_FLOOR",
        _SP,
        "Weekly floor (0-1)",
        "Share of each account's weekly window always held back from spare-capacity users.",
        "float",
        "live",
        minimum=0,
        maximum=1,
    ),
    # --- Fallbacks ---
    SettingSpec(
        "FALLBACK_GENERATION_CANARY_ENABLED",
        _F,
        "Generation canary",
        "Send a tiny generation request when health-checking an API fallback.",
        "bool",
        "live",
    ),
    SettingSpec(
        "FALLBACK_CANARY_MAX_OUTPUT_TOKENS", _F, "Canary max output tokens", "Output-token budget of the canary request.", "int", "live", minimum=1
    ),
    # --- Egress ---
    SettingSpec(
        "EGRESS_TARGETS_JSON",
        _E,
        "Egress targets",
        "JSON array of approved outbound paths (local source addresses or authenticated CONNECT relays). The first enabled "
        "target is the default; blank uses the server's own network." + _RELAY_NOTE,
        "json",
        "restart",
        json_shape="array",
        relay_restart=True,
    ),
    SettingSpec(
        "DEFAULT_EGRESS_MAX_CONCURRENCY",
        _E,
        "Default egress concurrency",
        "Concurrent requests per egress target unless the target sets its own.",
        "int",
        "restart",
        minimum=1,
    ),
    SettingSpec(
        "EGRESS_RELAY_USERNAME",
        _E,
        "Relay username",
        "Username the backend presents to the CONNECT relay." + _RELAY_NOTE,
        "string",
        "restart",
        relay_restart=True,
    ),
    SettingSpec(
        "EGRESS_RELAY_TOKEN",
        _E,
        "Relay token",
        "Shared relay token (at least 24 characters when relay targets are configured)." + _RELAY_NOTE,
        "secret",
        "restart",
        clearable=True,
        relay_restart=True,
    ),
    SettingSpec(
        "EGRESS_RELAY_HEALTH_PORT",
        _E,
        "Relay health port",
        "Health port of the separate host-network relay project." + _RELAY_NOTE,
        "int",
        "redeploy",
        minimum=1,
        maximum=65535,
        default="18080",
        relay_restart=True,
    ),
    SettingSpec(
        "EGRESS_RELAY_ALLOWED_HOSTS",
        _E,
        "Relay allowed hosts",
        "Comma-separated hosts the relay may CONNECT to (a leading dot allows subdomains)." + _RELAY_NOTE,
        "list",
        "redeploy",
        relay_restart=True,
    ),
    # --- Archive ---
    SettingSpec("ARCHIVE_ENABLED", _A, "Raw archive", "Write exact client-facing request/response bodies to S3/MinIO.", "bool", "restart"),
    SettingSpec("ARCHIVE_REQUIRED", _A, "Archive required", "Refuse proxy traffic when the archive cannot be written.", "bool", "restart"),
    SettingSpec(
        "ARCHIVE_S3_ENDPOINT_URL", _A, "S3 endpoint URL", "S3-compatible endpoint, e.g. http://minio:9000. Blank uses AWS.", "string", "restart"
    ),
    SettingSpec("ARCHIVE_S3_REGION", _A, "S3 region", "Bucket region.", "string", "restart"),
    SettingSpec("ARCHIVE_S3_BUCKET", _A, "S3 bucket", "Bucket the archive writes to.", "string", "restart"),
    SettingSpec("ARCHIVE_S3_PREFIX", _A, "S3 prefix", "Optional key prefix inside the bucket.", "string", "restart"),
    SettingSpec(
        "ARCHIVE_S3_ACCESS_KEY_ID",
        _A,
        "S3 access key ID",
        "Set both keys, or neither to use the default credential chain.",
        "secret",
        "restart",
        clearable=True,
    ),
    SettingSpec(
        "ARCHIVE_S3_SECRET_ACCESS_KEY",
        _A,
        "S3 secret access key",
        "Set both keys, or neither to use the default credential chain.",
        "secret",
        "restart",
        clearable=True,
    ),
    SettingSpec("ARCHIVE_S3_FORCE_PATH_STYLE", _A, "Path-style addressing", "Use path-style bucket URLs (needed for MinIO).", "bool", "restart"),
    SettingSpec("ARCHIVE_S3_MAX_ATTEMPTS", _A, "S3 max attempts", "Retry budget per archive write.", "int", "restart", minimum=1, maximum=20),
    SettingSpec(
        "ARCHIVE_HOT_RETENTION_DAYS",
        _A,
        "Hot retention (days)",
        "Days raw bodies stay in S3 before the retention worker moves them to Borg.",
        "int",
        "live",
        minimum=0,
    ),
    # --- Archive retention (read by the retention worker on every cycle) ---
    SettingSpec(
        "ARCHIVE_RETENTION_ENABLED", _AR, "Retention worker", "Move archived bodies older than the hot retention window into Borg.", "bool", "live"
    ),
    SettingSpec(
        "ARCHIVE_RETENTION_INTERVAL_SECONDS",
        _AR,
        "Retention interval (seconds)",
        "Pause between retention cycles (at least 60).",
        "int",
        "live",
        minimum=60,
    ),
    SettingSpec(
        "ARCHIVE_BORG_HOST_DIR",
        _AR,
        "Borg host directory",
        "Host directory bind-mounted at /borg in the retention container.",
        "string",
        "redeploy",
        default="/srv/claude-code-proxy/borg",
    ),
    SettingSpec("ARCHIVE_BORG_REPOSITORY", _AR, "Borg repository", "Repository path inside the retention container.", "string", "live"),
    SettingSpec(
        "ARCHIVE_RETENTION_WORK_DIR", _AR, "Work directory", "Scratch directory for batches inside the retention container.", "string", "live"
    ),
    SettingSpec(
        "ARCHIVE_RETENTION_BATCH_BYTES", _AR, "Batch size (bytes)", "Bytes collected before a Borg archive is written.", "int", "live", minimum=1
    ),
    SettingSpec(
        "ARCHIVE_RETENTION_MIN_FREE_GIB",
        _AR,
        "Minimum free space (GiB)",
        "Retention pauses when the work disk has less free space.",
        "int",
        "live",
        minimum=0,
    ),
    SettingSpec(
        "ARCHIVE_RETENTION_DELETE_MINIO",
        _AR,
        "Delete from S3 after archiving",
        "Delete objects from S3 once they are verified in Borg.",
        "bool",
        "live",
    ),
)

BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}
RESTART_KEYS: frozenset[str] = frozenset(spec.key for spec in SPECS if spec.apply == "restart")
REDEPLOY_KEYS: frozenset[str] = frozenset(spec.key for spec in SPECS if spec.apply == "redeploy")
SECRET_KEYS: frozenset[str] = frozenset(spec.key for spec in SPECS if spec.secret)
READ_ONLY_KEYS: frozenset[str] = frozenset(spec.key for spec in SPECS if spec.read_only)
RELAY_KEYS: frozenset[str] = frozenset(spec.key for spec in SPECS if spec.relay_restart)

# Substrings that mark an unregistered key's value as secret.
_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASS", "KEY", "CREDENTIAL")


def looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SECRET_MARKERS)
