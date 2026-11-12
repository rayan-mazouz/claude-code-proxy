# Path: app/config.py
# Description: Loads the editable `.env` config file (live-reloaded) and exposes a pydantic `BaseSettings` class plus the fixed proxy/OAuth constants.

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy.engine import URL

from app.utils import config_file

# The editable config file. CCP_CONFIG_FILE names it (the containers bind-mount the deployment's .env at
# /app/config/.env); without it the repo-root .env is used for local development. Values in this file win over the
# process environment, which Docker froze when the container was created, so dashboard edits take effect.
CONFIG_FILE_ENV = "CCP_CONFIG_FILE"
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# How often get_settings() re-stats the config file for changes.
RELOAD_CHECK_SECONDS = 2.0

_log = logging.getLogger(__name__)


def config_file_path() -> Path:
    raw = os.environ.get(CONFIG_FILE_ENV, "").strip()
    return Path(raw) if raw else _ENV_FILE


# Every effort level the Messages API accepts, lowest first; THINKING_LEVELS selects the ones this proxy offers.
KNOWN_THINKING_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Everything the backend serves is mounted under this path prefix; the dashboard lives at "/" and the backend at "/api"
# on the same Traefik host, so the proxy endpoint is /api/v1/messages. Both the router mount and the upstream-forwarding
# logic read this so they agree on the prefix.
API_PREFIX = "/api"

# Upstream (Anthropic) + Claude Code emulation. The claude-cli User-Agent keeps OAuth requests out of the aggressively
# rate-limited bucket; the oauth beta header is required for subscription tokens.
UPSTREAM_BASE_URL = "https://api.anthropic.com"
CLAUDE_CODE_USER_AGENT = "claude-cli/1.0.60 (external, cli)"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"

# OAuth (Claude Code subscription login). Well-known Claude Code client parameters, verified against the reference clients.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
OAUTH_SCOPE = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"

# Refresh an account's access token if it expires within this many seconds.
TOKEN_REFRESH_LEEWAY_SECONDS = 300

# Rotation defaults seeded onto every new account (the dashboard then tunes them per-account; these are the column
# defaults + the fallback when no account is in play). Rotation policy is per-account, not a global env knob.
DEFAULT_FIVE_HOUR_ROTATION_THRESHOLD = 1.0
DEFAULT_WEEKLY_ROTATION_THRESHOLD = 1.0
# Deprecated compatibility alias for older integrations.
DEFAULT_ROTATION_THRESHOLD = DEFAULT_FIVE_HOUR_ROTATION_THRESHOLD
DEFAULT_COOLDOWN_SECONDS = 60  # rest period after a 429 with no usable retry-after
DEFAULT_MAX_FAILOVER_ATTEMPTS = 3  # accounts to try per request
# TODO(concurrency calibration): measure the provider ceiling per model and
# subscription tier with controlled parallel probes, then make this policy
# model/tier-aware instead of relying on this conservative default.
DEFAULT_MAX_CONCURRENT_REQUESTS_PER_ACCOUNT = 3
DEFAULT_POOL_WAIT_TIMEOUT_SECONDS = 150
DEFAULT_POOL_WAIT_POLL_INTERVAL_SECONDS = 2
DEFAULT_TRANSIENT_UPSTREAM_COOLDOWN_SECONDS = 5


class Settings(BaseSettings):
    # Environment Configuration
    ENV: str = "production"

    # Logging verbosity (DEBUG/INFO/WARNING/ERROR). INFO is the production default; drop to DEBUG only for diagnosis.
    LOG_LEVEL: str = "INFO"

    # PostgreSQL Configuration
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str
    POSTGRES_PORT: str
    POSTGRES_DB: str

    def get_postgres_url(self) -> URL:
        # Built structurally so credentials containing URL metacharacters (@ : / % #) are escaped correctly.
        return URL.create(
            # psycopg 3, named explicitly so the driver never changes with SQLAlchemy's default.
            "postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=int(self.POSTGRES_PORT),
            database=self.POSTGRES_DB,
        )

    def get_postgres_uri(self) -> str:
        return self.get_postgres_url().render_as_string(hide_password=False)

    # Connection pool, per process. The backend runs several worker processes plus three sidecars against one
    # PostgreSQL server (100 connections by default), so the worst case -- every process at pool size plus
    # overflow -- must stay under that ceiling. Requests only hold a connection while they talk to the database.
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=100)
    DB_MAX_OVERFLOW: int = Field(default=10, ge=0, le=200)
    DB_POOL_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=600)

    # Secrets -- FERNET_KEY encrypts OAuth tokens at rest; ADMIN_* is the dashboard login; JWT_SECRET signs admin sessions.
    FERNET_KEY: str
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 12

    # How often the quota-refresher sidecar re-probes every account's usage and subscription tier.
    QUOTA_REFRESH_INTERVAL_SECONDS: int = 60
    # Warm-up is part of the normal pool-management loop. Deployments can
    # explicitly disable synthetic traffic with WARMUP_ENABLED=false.
    WARMUP_ENABLED: bool = True
    WARMUP_TRIGGER_POOL_USAGE_PCT: float = 0.10
    WARMUP_WEEKLY_RESERVE_PCT: float = 0.90
    WARMUP_MODEL: str = "claude-haiku-4-5"
    # Comma-separated local model allowlist. Blank keeps the built-in lineup.
    ALLOWED_MODELS: str = ""
    # Comma-separated effort levels the proxy recognizes and the dashboard offers, lowest first. A request naming any
    # other level is refused. Add "xhigh" (between high and max) to make it selectable for users and presets.
    THINKING_LEVELS: str = "low,medium,high,max"

    @field_validator("THINKING_LEVELS")
    @classmethod
    def _check_thinking_levels(cls, value: str) -> str:
        """Fail at load time (keeping the previous settings on a live reload) instead of on every request."""
        levels = [item.strip().lower() for item in value.split(",") if item.strip()]
        unknown = sorted(set(levels) - set(KNOWN_THINKING_LEVELS))
        if unknown:
            raise ValueError(f"unknown effort level(s): {', '.join(unknown)}; known: {', '.join(KNOWN_THINKING_LEVELS)}")
        return value

    # Default proxy rate limit (requests/minute) for an API key with no explicit override; 0 disables limiting.
    DEFAULT_KEY_RATE_LIMIT_PER_MINUTE: int = 0
    MAX_CONCURRENT_REQUESTS_PER_ACCOUNT: int = DEFAULT_MAX_CONCURRENT_REQUESTS_PER_ACCOUNT
    MODEL_CONCURRENCY_LIMITS_JSON: str = ""
    TIER_CONCURRENCY_LIMITS_JSON: str = ""
    POOL_WAIT_TIMEOUT_SECONDS: int = DEFAULT_POOL_WAIT_TIMEOUT_SECONDS
    POOL_WAIT_POLL_INTERVAL_SECONDS: float = DEFAULT_POOL_WAIT_POLL_INTERVAL_SECONDS
    TRANSIENT_UPSTREAM_COOLDOWN_SECONDS: int = DEFAULT_TRANSIENT_UPSTREAM_COOLDOWN_SECONDS
    FALLBACK_GENERATION_CANARY_ENABLED: bool = True
    # Share of each account's 5-hour / weekly window always held back from spare-capacity users, even when
    # nobody else has used that window yet (0..1). See app/utils/spare_capacity.py.
    SPARE_CAPACITY_FIVE_HOUR_FLOOR: float = Field(default=0.10, ge=0.0, le=1.0)
    SPARE_CAPACITY_WEEKLY_FLOOR: float = Field(default=0.30, ge=0.0, le=1.0)
    FALLBACK_CANARY_MAX_OUTPUT_TOKENS: int = 1

    # Optional outbound paths. Each target is either a local source address or
    # an authenticated CONNECT relay; accounts may pin to a target by ID.
    EGRESS_TARGETS_JSON: str = ""
    DEFAULT_EGRESS_MAX_CONCURRENCY: int = 32
    EGRESS_RELAY_USERNAME: str = "proxy"
    EGRESS_RELAY_TOKEN: Optional[str] = None

    # Exact client-facing request/response bodies go directly to S3/MinIO.
    ARCHIVE_ENABLED: bool = False
    ARCHIVE_REQUIRED: bool = True
    ARCHIVE_S3_ENDPOINT_URL: Optional[str] = None
    ARCHIVE_S3_REGION: str = "us-east-1"
    ARCHIVE_S3_BUCKET: str = "cc-proxy"
    ARCHIVE_S3_PREFIX: str = ""
    ARCHIVE_S3_ACCESS_KEY_ID: Optional[str] = None
    ARCHIVE_S3_SECRET_ACCESS_KEY: Optional[str] = None
    ARCHIVE_S3_FORCE_PATH_STYLE: bool = True
    ARCHIVE_S3_MAX_ATTEMPTS: int = 4
    ARCHIVE_HOT_RETENTION_DAYS: int = 4
    ARCHIVE_RETENTION_ENABLED: bool = False
    ARCHIVE_RETENTION_INTERVAL_SECONDS: int = 3600
    ARCHIVE_BORG_REPOSITORY: str = "/borg/repository"
    ARCHIVE_RETENTION_WORK_DIR: str = "/borg/work"
    ARCHIVE_RETENTION_BATCH_BYTES: int = 536870912
    ARCHIVE_RETENTION_MIN_FREE_GIB: int = 10
    ARCHIVE_RETENTION_DELETE_MINIO: bool = False

    # Deployment Configuration -- full domain the proxy is served on; the dashboard is at https://{DOMAIN}/ and the
    # backend at https://{DOMAIN}/api.
    DOMAIN: str

    # CORS allow-origin for the dashboard (same origin as the proxy).
    @property
    def FRONTEND_ORIGIN(self) -> str:
        return f"https://{self.DOMAIN}"

    @model_validator(mode="after")
    def _reject_unsafe_secrets(self) -> "Settings":
        """Refuse to start with placeholder or weak secrets: a guessable JWT_SECRET lets anyone forge an admin session."""
        placeholders = {"", "change-me", "change-me-please", "change-me-too", "changeme", "devpassword", "password", "secret", "admin"}
        if self.JWT_SECRET.strip().lower() in placeholders or len(self.JWT_SECRET) < 32:
            raise ValueError("JWT_SECRET is unset, a placeholder, or shorter than 32 characters; set a strong random value (see .env.example).")
        if self.ADMIN_PASSWORD.strip().lower() in placeholders or len(self.ADMIN_PASSWORD) < 8:
            raise ValueError("ADMIN_PASSWORD is unset, a placeholder, or shorter than 8 characters; set a strong value (see .env.example).")
        if not self.FERNET_KEY.strip():
            raise ValueError(
                'FERNET_KEY is unset; generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        if self.ARCHIVE_ENABLED:
            if not self.ARCHIVE_S3_BUCKET.strip():
                raise ValueError("ARCHIVE_S3_BUCKET must be set when ARCHIVE_ENABLED=true.")
            credentials = (self.ARCHIVE_S3_ACCESS_KEY_ID, self.ARCHIVE_S3_SECRET_ACCESS_KEY)
            if any(credentials) and not all(credentials):
                raise ValueError("Set both ARCHIVE_S3_ACCESS_KEY_ID and ARCHIVE_S3_SECRET_ACCESS_KEY, or neither.")
            if self.ARCHIVE_S3_MAX_ATTEMPTS < 1:
                raise ValueError("ARCHIVE_S3_MAX_ATTEMPTS must be at least 1.")
            self.ARCHIVE_S3_PREFIX = self.ARCHIVE_S3_PREFIX.strip("/")
        return self

    # extra="ignore" so deploy-only keys in .env (HTTP_PORT, TRAEFIK_*, POSTGRES_*) don't fail backend startup.
    model_config = SettingsConfigDict(env_file=str(config_file_path()), env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The config file beats the process environment: the container environment is a stale copy of the file
        # taken when the container was created, while the file is what the operator (or the dashboard) edits.
        return (init_settings, dotenv_settings, env_settings, file_secret_settings)

    def concurrency_limit_for(self, model: str | None = None, tier: str | None = None) -> int:
        for raw, key in ((self.MODEL_CONCURRENCY_LIMITS_JSON, model), (self.TIER_CONCURRENCY_LIMITS_JSON, tier)):
            if not raw.strip() or not key:
                continue
            try:
                values = json.loads(raw)
                value = values.get(key) if isinstance(values, dict) else None
                if value is not None:
                    return max(1, min(int(value), 32))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return max(1, min(int(self.MAX_CONCURRENT_REQUESTS_PER_ACCOUNT), 32))


def file_field_values(values: dict[str, str]) -> dict[str, str]:
    """The subset of raw config-file values that are Settings fields (keys matched case-insensitively)."""
    fields = {name.upper(): name for name in Settings.model_fields}
    return {fields[key.upper()]: value for key, value in values.items() if key.upper() in fields}


def build_settings(file_values: Optional[dict[str, str]] = None) -> Settings:
    """Build Settings from config-file values (read from disk when omitted) layered over the process environment.

    The file is read once, under a shared lock, and passed as init values, which outrank the environment; the
    model validators (including the unsafe-secret guard) run as usual.
    """
    if file_values is None:
        file_values = config_file.read_values(config_file_path())
    return Settings(_env_file=None, **cast(dict[str, Any], file_field_values(file_values)))


def _error_fields(exc: ValidationError) -> str:
    # Never log str(exc): pydantic includes the input values, which hold secrets.
    parts = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "settings"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


class _SettingsState:
    """Process-wide current Settings, rebuilt when the config file changes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.current: Optional[Settings] = None
        self.startup: Optional[Settings] = None
        self.signature: Optional[tuple] = None
        self.failed_signature: Optional[tuple] = None
        self.unreadable = False
        self.checked_at = 0.0

    def get(self) -> Settings:
        current = self.current
        if current is not None and time.monotonic() - self.checked_at < RELOAD_CHECK_SECONDS:
            return current
        with self.lock:
            now = time.monotonic()
            if self.current is not None and now - self.checked_at < RELOAD_CHECK_SECONDS:
                return self.current
            self.checked_at = now
            path = config_file_path()
            signature = config_file.signature(path)
            if self.current is not None and signature in (self.signature, self.failed_signature):
                return self.current
            try:
                file_values = config_file.read_values(path)
            except OSError as exc:
                # Unreadable (permissions, I/O error): not the same as an empty config. Retry on every check (a chmod
                # does not change the signature), but only warn when the file first becomes unreadable.
                reason = exc.strerror or type(exc).__name__
                if self.current is not None:
                    if not self.unreadable:
                        _log.warning("Config file %s is not readable (%s); keeping the current settings", path, reason)
                    self.unreadable = True
                    return self.current
                _log.warning("Config file %s is not readable (%s); using the process environment only", path, reason)
                settings = build_settings({})
                self.current = self.startup = settings
                self.signature = None
                self.unreadable = True
                return settings
            try:
                settings = build_settings(file_values)
            except (ValidationError, ValueError) as exc:
                if self.current is None:
                    raise
                self.failed_signature = signature
                self.unreadable = False
                detail = _error_fields(exc) if isinstance(exc, ValidationError) else type(exc).__name__
                _log.warning("Config file %s changed but is invalid; keeping the previous settings (%s)", path, detail)
                return self.current
            if self.current is not None:
                _log.info("Reloaded settings from %s", path)
            self.current = settings
            self.signature = signature
            self.failed_signature = None
            self.unreadable = False
            if self.startup is None:
                self.startup = settings
            return settings


_STATE = _SettingsState()


class LiveSettings:
    """A stable handle on the current Settings.

    Modules keep ``settings = get_settings()`` at import time; every attribute read goes to the newest valid
    Settings, so edits to the config file reach them within RELOAD_CHECK_SECONDS. Attribute writes (tests'
    monkeypatch) go to the current instance.
    """

    __slots__ = ()

    @property  # type: ignore[misc]
    def __class__(self):  # noqa: D105 -- isinstance(get_settings(), Settings) keeps working
        return Settings

    def __getattr__(self, name: str) -> Any:
        return getattr(_STATE.get(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(_STATE.get(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(_STATE.get(), name)

    def __repr__(self) -> str:
        return "LiveSettings(<hidden>)"


_LIVE = LiveSettings()


def get_settings() -> Settings:
    """Return the live settings handle (values follow the config file; see LiveSettings)."""
    _STATE.get()  # build eagerly so configuration errors surface at the call site, as before
    return cast(Settings, _LIVE)


def _cache_clear() -> None:
    """Forget the loaded settings; the next access rebuilds them from the file and environment."""
    with _STATE.lock:
        _STATE.reset()


get_settings.cache_clear = _cache_clear  # type: ignore[attr-defined]


def reload_settings() -> Settings:
    """Re-read the config file now instead of waiting for the next periodic check."""
    with _STATE.lock:
        _STATE.checked_at = 0.0
        _STATE.signature = None
        _STATE.failed_signature = None
    return _STATE.get()


def current_settings() -> Settings:
    """The concrete current Settings instance (not the live handle)."""
    return _STATE.get()


def startup_settings() -> Settings:
    """The Settings this process started with; restart-class keys keep these values until the process restarts."""
    _STATE.get()
    assert _STATE.startup is not None
    return _STATE.startup


def setting_as_text(settings: Settings, key: str) -> str:
    """Render a Settings field the way it would be written to .env."""
    value = getattr(settings, key)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def pending_restart_keys(keys: frozenset[str] | set[str]) -> list[str]:
    """Settings fields among ``keys`` whose current value differs from the value this process started with."""
    current, startup = current_settings(), startup_settings()
    return sorted(key for key in keys if key in Settings.model_fields and getattr(current, key) != getattr(startup, key))
