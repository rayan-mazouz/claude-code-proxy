# Path: app/config.py
# Description: Loads the global `.env` file and exposes a pydantic `BaseSettings` class plus the fixed proxy/OAuth constants.

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env, loaded for local development; in containers the values come from the process environment.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

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

    def get_postgres_uri(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

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
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

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


@lru_cache
def get_settings() -> Settings:
    """Get settings from .env file."""
    return Settings()
