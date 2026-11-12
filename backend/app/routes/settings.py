# Path: app/routes/settings.py
# Description: Owner-only API to view and edit the deployment's .env config file, and to restart the backend.

"""Dashboard Settings API.

GET returns every registered key with its current value (secrets masked), any other key found in the file
(read-only, group "Other"), whether the file is readable and writable, and the keys whose new value still waits for
a restart, a redeploy or an egress relay restart. PUT validates a partial update against a candidate Settings built
from the merged file, then rewrites the file in place; changing a security, database or egress key, or any secret,
needs the current owner password. POST /restart asks the gunicorn master for a graceful rolling restart of the
workers.
"""

from __future__ import annotations

import errno
import hmac
import json
import os
import posixpath
import re
import signal
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, ValidationError
from starlette.background import BackgroundTask

from app import config
from app.logger import get_logger
from app.utils import config_file, config_registry, egress, security
from app.utils.config_registry import BY_KEY, GROUPS, OTHER_GROUP, RELAY_RESTART_COMMAND, SPECS, SettingSpec
from app.utils.thinking import parse_thinking_levels

router = APIRouter(tags=["Settings"], prefix="/settings")
logger = get_logger()

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_INT = re.compile(r"^[+-]?\d+$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:\[\]-]+$")
_HOST = re.compile(r"^\.?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*$")

# The same checks scripts/server-install.sh applies to its CCP_* overrides: these values end up in Compose port
# mappings, Traefik rule labels and bind mounts, where anything else breaks (or rewrites) the deployment.
_PLAIN_PORT = re.compile(r"^[0-9]{1,5}$")
_IPV4 = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")
_EMAIL = re.compile(rf"^[A-Za-z0-9._%+-]+@{_LABEL}(\.{_LABEL})+$")
_PORT_KEYS = frozenset({"HTTP_PORT", "HTTPS_PORT", "EGRESS_RELAY_HEALTH_PORT"})
_SYSTEM_DIRS = frozenset({"proc", "sys", "dev", "run", "etc", "boot", "usr", "bin", "sbin"})

_PASSWORD_REQUIRED = "Enter the current owner password to change: {keys}."
_PASSWORD_WRONG = "The current owner password is incorrect."


# --- schemas -----------------------------------------------------------------


class SettingEntry(BaseModel):
    key: str
    group: str
    label: str
    help: str
    type: str
    apply: Literal["live", "restart", "redeploy"]
    choices: list[str] = Field(default_factory=list)
    read_only: bool
    secret: bool
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # Current value as text; always null for secrets.
    value: Optional[str] = None
    # Whether a secret (or any value) is set.
    is_set: bool
    # Where the effective value comes from: the config file, the process environment, or the built-in default.
    source: Literal["file", "environment", "default"]
    # Changing it needs the current owner password (PUT current_password).
    requires_password: bool = False
    # An optional secret the dashboard may clear by sending "".
    clearable: bool = False
    # Also read by the egress relay, which only picks changes up when it is restarted.
    relay_restart: bool = False


class SettingsState(BaseModel):
    path: str
    exists: bool
    readable: bool
    writable: bool
    groups: list[str]
    settings: list[SettingEntry]
    pending_restart: list[str]
    pending_redeploy: list[str]
    # Relay keys whose saved value differs from what the stack was deployed with.
    pending_relay: list[str]
    relay_restart_command: str
    restart_supported: bool


class UpdateSettingsRequest(BaseModel):
    values: dict[str, Union[bool, int, float, str]] = Field(min_length=1)
    # Required when a change touches a key with requires_password. Never logged.
    current_password: Optional[SecretStr] = None


class RestartResponse(BaseModel):
    status: Literal["restarting"]


# --- helpers -----------------------------------------------------------------


def _effective(spec: SettingSpec, file_values: dict[str, str]) -> tuple[str, str]:
    """Return (value, source) for a key as this process sees it."""
    if spec.key in file_values:
        return file_values[spec.key], "file"
    if spec.key in config.Settings.model_fields:
        source = "environment" if spec.key in os.environ else "default"
        return config.setting_as_text(config.current_settings(), spec.key), source
    if spec.key in os.environ:
        return os.environ[spec.key], "environment"
    return spec.default or "", "default"


def _redeploy_baseline(key: str, startup_file: dict[str, str]) -> str:
    # Inside the containers, Compose copied the file into the environment when it created them, so the
    # environment is what the running stack was deployed with. Locally (no CCP_CONFIG_FILE) compare with the file
    # as it was when this process started.
    if os.environ.get(config.CONFIG_FILE_ENV):
        return os.environ.get(key, "")
    return startup_file.get(key, "")


_STARTUP_FILE: Optional[dict[str, str]] = None


def _read_file(path: Path) -> tuple[dict[str, str], bool]:
    """(values, readable). An unreadable file reports no values instead of failing the page."""
    try:
        return config_file.read_values(path), True
    except (OSError, ValueError):
        return {}, False


def _startup_file_values() -> dict[str, str]:
    global _STARTUP_FILE
    if _STARTUP_FILE is None:
        values, readable = _read_file(config.config_file_path())
        if not readable:
            return values
        _STARTUP_FILE = values
    return _STARTUP_FILE


def _gunicorn_master_pid() -> Optional[int]:
    """The parent PID when this worker's parent really is a gunicorn master, else None."""
    parent = os.getppid()
    if parent <= 1:
        return None
    try:
        parts = Path(f"/proc/{parent}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    for part in parts:
        name = Path(part.decode(errors="ignore")).name
        if name.startswith("gunicorn"):
            return parent
    return None


def _other_entry(key: str, value: str) -> SettingEntry:
    """A key found in the file that the registry does not describe: shown, never edited."""
    secret = config_registry.looks_secret(key)
    return SettingEntry(
        key=key,
        group=OTHER_GROUP,
        label=key,
        help="Not managed by the dashboard. Edit the config file by hand to change it.",
        type="secret" if secret else "string",
        apply="redeploy",
        read_only=True,
        secret=secret,
        value=None if secret else value,
        is_set=bool(value),
        source="file",
    )


def _state() -> SettingsState:
    _startup_file_values()
    path = config.config_file_path()
    file_values, readable = _read_file(path)
    entries = []
    for spec in SPECS:
        value, source = _effective(spec, file_values)
        entries.append(
            SettingEntry(
                key=spec.key,
                group=spec.group,
                label=spec.label,
                help=spec.help,
                type=spec.type,
                apply=spec.apply,
                choices=list(spec.choices),
                read_only=spec.read_only,
                secret=spec.secret,
                minimum=spec.minimum,
                maximum=spec.maximum,
                value=None if spec.secret else value,
                is_set=bool(value),
                source=source,  # type: ignore[arg-type]
                requires_password=spec.requires_password and not spec.read_only,
                clearable=spec.clearable,
                relay_restart=spec.relay_restart,
            )
        )
    others = [_other_entry(key, value) for key, value in file_values.items() if key not in BY_KEY]
    entries.extend(others)
    startup_file = _startup_file_values()

    def differs(key: str) -> bool:
        return key in file_values and file_values[key] != _redeploy_baseline(key, startup_file)

    return SettingsState(
        path=str(path),
        exists=path.exists(),
        readable=readable,
        writable=readable and config_file.is_writable(path),
        groups=[*GROUPS, OTHER_GROUP] if others else list(GROUPS),
        settings=entries,
        pending_restart=config.pending_restart_keys(config_registry.RESTART_KEYS),
        pending_redeploy=sorted(key for key in config_registry.REDEPLOY_KEYS if differs(key)),
        pending_relay=sorted(key for key in config_registry.RELAY_KEYS if differs(key)),
        relay_restart_command=RELAY_RESTART_COMMAND,
        restart_supported=_gunicorn_master_pid() is not None,
    )


def _as_text(value: Union[bool, int, float, str]) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _check_host_dir(value: str) -> None:
    if not value.startswith("/"):
        raise ValueError("Must be an absolute path.")
    if value.startswith("//") or posixpath.normpath(value) != value:
        raise ValueError("Must be a normalized path (no //, /./, /../ or trailing /).")
    if value == "/":
        raise ValueError("Cannot be the root directory.")
    first = value.split("/")[1]
    if first in _SYSTEM_DIRS or first.startswith("lib") or value == "/var/run" or value.startswith("/var/run/"):
        raise ValueError("Cannot be a system directory (/proc, /sys, /dev, /run, /etc, /boot, /usr, /bin, /sbin, /lib*, /var/run).")


def _check_compose_value(key: str, value: str) -> None:
    """Key-specific checks for values Docker Compose and Traefik consume."""
    if key in _PORT_KEYS:
        if not _PLAIN_PORT.match(value) or not 1 <= int(value) <= 65535:
            raise ValueError("Must be a port number between 1 and 65535 (digits only).")
    elif key == "HTTP_BIND_ADDRESS":
        if value and (not _IPV4.match(value) or any(int(octet) > 255 for octet in value.split("."))):
            raise ValueError("Must be an IPv4 address such as 0.0.0.0 or 10.8.0.1.")
    elif key == "DOMAIN":
        if not value:
            raise ValueError("Required.")
        if len(value) > 253 or not _HOSTNAME.match(value):
            raise ValueError("Must be a plain hostname such as proxy.example.com (no scheme, port or path).")
    elif key == "TRAEFIK_ENTRYPOINT":
        if value not in ("web", "websecure"):
            raise ValueError("Must be web or websecure.")
    elif key == "ACME_EMAIL":
        if value and (len(value) > 254 or not _EMAIL.match(value)):
            raise ValueError("Must be a plain email address such as ops@example.com.")
    elif key == "ARCHIVE_BORG_HOST_DIR":
        _check_host_dir(value)


def _normalize(spec: SettingSpec, raw: str) -> str:
    """Validate one value against its registry type and return the text to write."""
    value = _normalize_type(spec, raw)
    _check_compose_value(spec.key, value)
    return value


def _normalize_type(spec: SettingSpec, raw: str) -> str:
    value = raw if spec.type == "secret" else raw.strip()
    if spec.type == "secret":
        if not value and not spec.clearable:
            raise ValueError("Required; enter a value.")
        return value
    if spec.type == "bool":
        lowered = value.lower()
        if lowered in _TRUE:
            return "true"
        if lowered in _FALSE:
            return "false"
        raise ValueError("Must be true or false.")
    if spec.type in ("int", "float"):
        if spec.type == "int":
            if not _INT.match(value):
                raise ValueError("Must be a whole number.")
            number: float = int(value)
        else:
            try:
                number = float(value)
            except ValueError:
                raise ValueError("Must be a number.") from None
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError("Must be a finite number.")
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"Must be at least {spec.minimum:g}.")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"Must be at most {spec.maximum:g}.")
        return value
    if spec.type == "choice":
        if value not in spec.choices:
            raise ValueError(f"Must be one of: {', '.join(spec.choices)}.")
        return value
    if spec.type == "json":
        if not value:
            return ""
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).") from None
        if spec.json_shape == "object" and not isinstance(parsed, dict):
            raise ValueError("Must be a JSON object.")
        if spec.json_shape == "array" and not isinstance(parsed, list):
            raise ValueError("Must be a JSON array.")
        # Re-serialize compactly so the value always fits on one line.
        return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    if spec.type == "list":
        items = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
        if spec.key == "ALLOWED_MODELS" and any(not _MODEL_ID.match(item) for item in items):
            raise ValueError("Model IDs may only contain letters, digits and . _ : - [ ].")
        if spec.key == "EGRESS_RELAY_ALLOWED_HOSTS" and any(not _HOST.match(item) for item in items):
            raise ValueError("Hosts must be plain hostnames, optionally with a leading dot.")
        if spec.key == "THINKING_LEVELS":
            parse_thinking_levels(",".join(items))
        return ",".join(items)
    return value


def _invalid(errors: dict[str, str]) -> HTTPException:
    detail = [{"loc": ["body", "values", key], "msg": message, "type": "value_error"} for key, message in errors.items()]
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _settings_errors(exc: ValidationError, changed: list[str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        message = str(error.get("msg", "Invalid value")).removeprefix("Value error, ")
        location = error.get("loc") or ()
        key = str(location[0]) if location and str(location[0]) in BY_KEY else None
        if key is None:
            # Model-level checks (e.g. the unsafe-secret guard) name the key in their message.
            key = next((name for name in changed if name in message), None) or next((name for name in BY_KEY if name in message), "_form")
        errors.setdefault(key, message)
    return errors


def _cross_checks(merged: dict[str, str], changed: list[str], errors: dict[str, str]) -> None:
    """Checks that span several keys and are not part of the Settings model."""
    if {"EGRESS_TARGETS_JSON", "DEFAULT_EGRESS_MAX_CONCURRENCY", "EGRESS_RELAY_TOKEN"} & set(changed):
        current = config.current_settings()
        raw = merged.get("EGRESS_TARGETS_JSON", current.EGRESS_TARGETS_JSON or "")
        try:
            concurrency = int(merged.get("DEFAULT_EGRESS_MAX_CONCURRENCY", current.DEFAULT_EGRESS_MAX_CONCURRENCY))
            targets = egress.parse_targets(raw, concurrency)
        except (ValueError, TypeError) as exc:
            errors.setdefault("EGRESS_TARGETS_JSON", str(exc))
            return
        token = merged.get("EGRESS_RELAY_TOKEN", current.EGRESS_RELAY_TOKEN or "")
        if any(target.relay_auth for target in targets) and len(token) < 24:
            errors.setdefault("EGRESS_RELAY_TOKEN", "Must contain at least 24 characters when a target uses relay_auth.")


def _require_password(provided: Optional[SecretStr], keys: list[str]) -> None:
    """403 unless ``provided`` is the current owner password (checked only when a sensitive key changes)."""
    if not keys:
        return
    if provided is None or not provided.get_secret_value():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_PASSWORD_REQUIRED.format(keys=", ".join(sorted(keys))))
    expected = config.current_settings().ADMIN_PASSWORD
    if not hmac.compare_digest(provided.get_secret_value().encode("utf-8"), expected.encode("utf-8")):
        logger.warning("Settings change to %s refused: wrong owner password", ", ".join(sorted(keys)))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_PASSWORD_WRONG)


# --- routes ------------------------------------------------------------------


@router.get("", response_model=SettingsState)
def get_settings_state(_: str = Depends(security.require_admin)) -> SettingsState:  # noqa: B008
    config.current_settings()  # pick up a recent edit before reporting pending keys
    return _state()


@router.put("", response_model=SettingsState)
def update_settings(
    request: UpdateSettingsRequest,
    subject: str = Depends(security.require_admin),  # noqa: B008
) -> SettingsState:
    path = config.config_file_path()
    file_values, readable = _read_file(path)
    if not readable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The config file {path} is not readable by the backend process.",
        )
    errors: dict[str, str] = {}
    updates: dict[str, str] = {}
    for key, raw in request.values.items():
        spec = BY_KEY.get(key)
        if spec is None:
            errors[key] = "Not managed by the dashboard; edit the config file by hand." if key in file_values else "Unknown setting."
            continue
        if spec.read_only:
            errors[key] = "This setting is read-only in the dashboard; edit the config file by hand."
            continue
        try:
            value = _normalize(spec, _as_text(raw))
            config_file.format_value(value)
        except ValueError as exc:
            errors[key] = str(exc)
            continue
        updates[key] = value
    if errors:
        raise _invalid(errors)

    changed = [key for key, value in updates.items() if file_values.get(key) != value]
    _require_password(request.current_password, [key for key in changed if BY_KEY[key].requires_password])
    merged = {**file_values, **updates}
    _cross_checks(merged, changed, errors)
    try:
        config.build_settings(merged)
    except ValidationError as exc:
        errors.update({key: message for key, message in _settings_errors(exc, list(updates)).items() if key not in errors})
    if errors:
        raise _invalid(errors)

    if not changed:
        return _state()
    if not config_file.is_writable(path):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The config file {path} is not writable by the backend process.",
        )
    try:
        config_file.write_values(path, {key: updates[key] for key in changed})
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The config file {path} is not writable by the backend process.",
        ) from None
    except OSError as exc:
        no_space = exc.errno in (errno.ENOSPC, errno.EDQUOT)
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE if no_space else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not write the config file {path}: {exc.strerror or type(exc).__name__}." + (" It was left unchanged." if no_space else ""),
        ) from None
    config.reload_settings()
    logger.info("Settings updated by %s: %s", subject, ", ".join(sorted(changed)))
    return _state()


@router.post("/restart", response_model=RestartResponse, status_code=status.HTTP_202_ACCEPTED)
def restart_backend(subject: str = Depends(security.require_admin)) -> JSONResponse:  # noqa: B008
    master = _gunicorn_master_pid()
    if master is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The backend is not running under gunicorn; restart it manually.",
        )
    logger.info("Rolling backend restart requested by %s", subject)
    # SIGHUP makes gunicorn start fresh workers and gracefully stop the old ones (including this one, after it
    # has sent this response), so the backend keeps serving throughout.
    return JSONResponse(
        {"status": "restarting"},
        status_code=status.HTTP_202_ACCEPTED,
        background=BackgroundTask(os.kill, master, signal.SIGHUP),
    )
