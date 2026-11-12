"""Dashboard Settings: in-place .env rewriting, quoting, validation, masking, live reload and permissions."""

import errno
import logging
import os
import signal
import stat
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app import config
from app.routes import settings as settings_route
from app.utils import config_file, config_watch
from app.utils.config_registry import BY_KEY, RELAY_KEYS, RESTART_KEYS, SPECS

OWNER_PASSWORD = "test-admin-password-123"  # conftest's ADMIN_PASSWORD


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point the app at a scratch config file and rebuild settings around the test."""
    path = tmp_path / ".env"
    path.write_text("# scratch\n", encoding="utf-8")
    monkeypatch.setenv("CCP_CONFIG_FILE", str(path))
    monkeypatch.setattr(config, "RELOAD_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(settings_route, "_STARTUP_FILE", None)
    config.get_settings.cache_clear()
    yield path
    config.get_settings.cache_clear()


def _bump(path: Path) -> None:
    """Make sure the change detector sees a new mtime even on coarse-timestamp filesystems."""
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))


def _put(client, headers, values: dict, password: str | None = OWNER_PASSWORD):
    body: dict = {"values": values}
    if password is not None:
        body["current_password"] = password
    return client.put("/api/v1/settings", headers=headers, json=body)


def _unreadable(monkeypatch) -> None:
    """Make the config file unreadable (works even when the tests run as root)."""

    def deny(_path):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(config_file, "read_text", deny)


def _entry(state: dict, key: str) -> dict:
    return next(item for item in state["settings"] if item["key"] == key)


# --- file writer -------------------------------------------------------------


def test_write_preserves_comments_order_and_appends(tmp_path):
    path = tmp_path / ".env"
    original = (
        "# Header comment\n"
        "\n"
        "ENV=production\n"
        "# LOG_LEVEL=DEBUG (commented out)\n"
        "export WARMUP_MODEL=old-model\n"
        'EGRESS_TARGETS_JSON="[\n'
        '  {\\"id\\": \\"x\\"}\n'
        ']"\n'
        "ALLOWED_MODELS=a\n"
        "TAIL=keep # inline\n"
        "ALLOWED_MODELS=dupe\n"
    )
    path.write_text(original, encoding="utf-8")
    inode = path.stat().st_ino

    changed = config_file.write_values(
        path,
        {"WARMUP_MODEL": "new-model", "EGRESS_TARGETS_JSON": "", "ALLOWED_MODELS": "m1,m2", "NEW_KEY": "added"},
    )

    assert changed is True
    assert path.stat().st_ino == inode  # rewritten in place, not replaced
    assert path.read_text(encoding="utf-8") == (
        "# Header comment\n"
        "\n"
        "ENV=production\n"
        "# LOG_LEVEL=DEBUG (commented out)\n"
        "export WARMUP_MODEL=new-model\n"
        "EGRESS_TARGETS_JSON=\n"
        "ALLOWED_MODELS=m1,m2\n"
        "TAIL=keep # inline\n"
        "NEW_KEY=added\n"
    )
    assert config_file.write_values(path, {"WARMUP_MODEL": "new-model"}) is False


def test_write_appends_newline_when_file_lacks_one(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1", encoding="utf-8")
    config_file.write_values(path, {"B": "2"})
    assert path.read_text(encoding="utf-8") == "A=1\nB=2\n"


@pytest.mark.parametrize(
    "value",
    [
        "simple",
        "",
        "claude-opus-5-5,claude-haiku-5-5",
        '{"claude-opus-5-5":2}',
        '[{"id": "eu", "kind": "proxy", "proxy_url": "http://relay:1"}]',
        "with space",
        " leading and trailing ",
        "hash#inside",
        "dollar$sign",
        "it's",
        "it's a # test",
        'say "hi"',
        "'starts-with-quote",
        '"starts-with-double',
        "https://example.com/path?q=1&r=2",
        "tab\tinside",
    ],
)
def test_quoting_round_trips_through_python_dotenv(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text("# c\nOTHER=1\n", encoding="utf-8")
    config_file.write_values(path, {"KEY": value})
    parsed = dotenv_values(path)
    assert parsed["KEY"] == value
    assert parsed["OTHER"] == "1"


@pytest.mark.parametrize("value", ["line\nbreak", "carriage\rreturn", "back\\slash", "mix'of$quotes\"", "it's $5", "${INTERPOLATED}", "pre${X}post"])
def test_unportable_values_are_rejected(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text("KEY=old\n", encoding="utf-8")
    with pytest.raises(config_file.ConfigValueError):
        config_file.write_values(path, {"KEY": value})
    assert path.read_text(encoding="utf-8") == "KEY=old\n"


def test_write_reserves_space_before_truncating(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    original = "A=" + "x" * 100 + "\nB=2\n"
    path.write_text(original, encoding="utf-8")
    calls = []

    def full_disk(descriptor, offset, length):
        calls.append((offset, length))
        os.ftruncate(descriptor, os.fstat(descriptor).st_size + 10)  # a partial extension before failing
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(config_file.os, "posix_fallocate", full_disk, raising=False)
    with pytest.raises(OSError) as raised:
        config_file.write_values(path, {"A": "y" * 500})
    assert raised.value.errno == errno.ENOSPC
    assert calls and calls[0][0] == 0 and calls[0][1] > len(original)
    assert path.read_text(encoding="utf-8") == original  # untouched, partial extension undone

    def unsupported(_descriptor, _offset, _length):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(config_file.os, "posix_fallocate", unsupported, raising=False)
    assert config_file.write_values(path, {"A": "short"}) is True
    assert path.read_text(encoding="utf-8") == "A=short\nB=2\n"

    monkeypatch.delattr(config_file.os, "posix_fallocate", raising=False)
    assert config_file.write_values(path, {"B": "3"}) is True
    assert path.read_text(encoding="utf-8") == "A=short\nB=3\n"


def test_write_uses_real_fallocate_and_keeps_exact_size(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=" + "x" * 4000 + "\n", encoding="utf-8")
    config_file.write_values(path, {"A": "1"})
    assert path.read_bytes() == b"A=1\n"
    config_file.write_values(path, {"A": "z" * 5000})
    assert path.stat().st_size == len("A=" + "z" * 5000 + "\n")


def test_write_keeps_a_private_backup_when_the_directory_allows(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("SECRET_THING=old\n", encoding="utf-8")
    config_file.write_values(path, {"SECRET_THING": "new"})
    backup = config_file.backup_path(path)
    assert backup == tmp_path / ".env.bak"
    assert backup.read_text(encoding="utf-8") == "SECRET_THING=old\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    # Unchanged content does not rotate the backup.
    config_file.write_values(path, {"SECRET_THING": "new"})
    assert backup.read_text(encoding="utf-8") == "SECRET_THING=old\n"

    # Directory not writable (the container's bind-mount directory): skipped silently, the write still happens.
    backup.unlink()
    real_access = os.access
    monkeypatch.setattr(config_file.os, "access", lambda target, mode: False if Path(target) == tmp_path else real_access(target, mode))
    assert config_file.write_values(path, {"SECRET_THING": "newer"}) is True
    assert not backup.exists()
    assert dotenv_values(path)["SECRET_THING"] == "newer"


# --- registry ----------------------------------------------------------------


def test_registry_covers_every_settings_field():
    assert set(config.Settings.model_fields) <= set(BY_KEY)
    assert len(SPECS) == len(BY_KEY)
    assert BY_KEY["THINKING_LEVELS"].group == "Models"
    for key in ("FERNET_KEY", "POSTGRES_PASSWORD", "POSTGRES_USER", "JWT_SECRET"):
        assert BY_KEY[key].read_only and BY_KEY[key].secret
    for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB"):
        assert BY_KEY[key].read_only and not BY_KEY[key].secret
    for key in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "JWT_ALGORITHM", "JWT_EXPIRE_MINUTES", "DB_POOL_SIZE", "EGRESS_TARGETS_JSON"):
        assert BY_KEY[key].requires_password, key
    assert BY_KEY["ARCHIVE_S3_SECRET_ACCESS_KEY"].requires_password  # every secret
    for key in ("WARMUP_MODEL", "LOG_LEVEL", "DOMAIN", "ARCHIVE_ENABLED"):
        assert not BY_KEY[key].requires_password, key
    assert {key for key in BY_KEY if BY_KEY[key].clearable} == {"EGRESS_RELAY_TOKEN", "ARCHIVE_S3_ACCESS_KEY_ID", "ARCHIVE_S3_SECRET_ACCESS_KEY"}
    assert RELAY_KEYS == {
        "EGRESS_RELAY_TOKEN",
        "EGRESS_RELAY_USERNAME",
        "EGRESS_TARGETS_JSON",
        "EGRESS_RELAY_ALLOWED_HOSTS",
        "EGRESS_RELAY_HEALTH_PORT",
    }
    for key in RELAY_KEYS:
        assert "docker compose -f docker-compose.egress.yml up -d" in BY_KEY[key].help
    for key in ("ADMIN_PASSWORD", "EGRESS_RELAY_TOKEN", "ARCHIVE_S3_SECRET_ACCESS_KEY", "ARCHIVE_S3_ACCESS_KEY_ID"):
        assert BY_KEY[key].secret and not BY_KEY[key].read_only
    for key in ("HTTP_PORT", "HTTPS_PORT", "HTTP_BIND_ADDRESS", "DOMAIN", "TRAEFIK_ENTRYPOINT", "ACME_EMAIL", "ARCHIVE_BORG_HOST_DIR"):
        assert BY_KEY[key].apply == "redeploy"
    for key in ("DB_POOL_SIZE", "LOG_LEVEL", "EGRESS_TARGETS_JSON", "ARCHIVE_S3_BUCKET"):
        assert BY_KEY[key].apply == "restart"


# --- live settings -----------------------------------------------------------


def test_config_file_outranks_environment_and_reloads_live(env_file, monkeypatch):
    monkeypatch.setenv("WARMUP_MODEL", "from-environment")
    config.get_settings.cache_clear()
    handle = config.get_settings()
    assert handle.WARMUP_MODEL == "from-environment"
    assert isinstance(handle, config.Settings)

    env_file.write_text("WARMUP_MODEL=from-file\n", encoding="utf-8")
    _bump(env_file)
    assert handle.WARMUP_MODEL == "from-file"  # same handle, new value

    # An invalid edit keeps the previous settings instead of breaking the process.
    env_file.write_text("WARMUP_MODEL=still-old\nDB_POOL_SIZE=not-a-number\n", encoding="utf-8")
    _bump(env_file)
    assert handle.WARMUP_MODEL == "from-file"

    env_file.write_text("WARMUP_MODEL=fixed\n", encoding="utf-8")
    _bump(env_file)
    assert handle.WARMUP_MODEL == "fixed"


def test_unsafe_secret_guard_still_runs_on_reload(env_file):
    handle = config.get_settings()
    password = handle.ADMIN_PASSWORD
    env_file.write_text("ADMIN_PASSWORD=short\n", encoding="utf-8")
    _bump(env_file)
    assert handle.ADMIN_PASSWORD == password
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        config.build_settings({"ADMIN_PASSWORD": "short"})


def test_restart_class_change_is_pending_until_restart(env_file):
    config.get_settings()
    env_file.write_text("LOG_LEVEL=DEBUG\nWARMUP_MODEL=x\n", encoding="utf-8")
    _bump(env_file)
    assert config.get_settings().LOG_LEVEL == "DEBUG"
    assert config.get_settings().WARMUP_MODEL == "x"
    assert config.pending_restart_keys(RESTART_KEYS) == ["LOG_LEVEL"]


def test_sidecar_watcher_exits_on_restart_class_change(env_file, monkeypatch):
    monkeypatch.setattr(config_watch, "CHECK_SECONDS", 0.0)
    raw = config_watch.RestartWatcher(use_settings=False, log=lambda _message: None)
    typed = config_watch.RestartWatcher(log=lambda _message: None)
    env_file.write_text("WARMUP_MODEL=live-only\n", encoding="utf-8")
    _bump(env_file)
    raw.exit_if_changed()
    typed.exit_if_changed()

    env_file.write_text("DB_POOL_SIZE=7\n", encoding="utf-8")
    _bump(env_file)
    assert raw.changed() == ["DB_POOL_SIZE"]
    with pytest.raises(SystemExit) as exited:
        typed.exit_if_changed()
    assert exited.value.code == 0


# --- API ---------------------------------------------------------------------


def test_get_masks_secrets_and_reports_file_state(client, admin_headers, env_file):
    env_file.write_text("EGRESS_RELAY_TOKEN=super-secret-relay-token-value-123\nLOG_LEVEL=INFO\n", encoding="utf-8")
    _bump(env_file)
    response = client.get("/api/v1/settings", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "super-secret-relay-token-value-123" not in response.text
    assert config.get_settings().JWT_SECRET not in response.text
    assert config.get_settings().ADMIN_PASSWORD not in response.text
    assert body["path"] == str(env_file)
    assert body["writable"] is True
    token = _entry(body, "EGRESS_RELAY_TOKEN")
    assert token["value"] is None and token["is_set"] is True and token["source"] == "file"
    assert _entry(body, "JWT_SECRET")["read_only"] is True
    assert _entry(body, "JWT_SECRET")["value"] is None
    assert _entry(body, "LOG_LEVEL")["value"] == "INFO"
    assert _entry(body, "WARMUP_ENABLED")["source"] == "environment"
    assert body["restart_supported"] is False


def test_put_writes_file_and_reports_pending(client, admin_headers, env_file):
    client.get("/api/v1/settings", headers=admin_headers)
    response = _put(
        client,
        admin_headers,
        {
            "WARMUP_MODEL": "claude-haiku-5-5",
            "WARMUP_ENABLED": False,
            "LOG_LEVEL": "DEBUG",
            "HTTP_PORT": 9090,
            "MODEL_CONCURRENCY_LIMITS_JSON": '{ "claude-opus-5-5": 2 }',
            "EGRESS_RELAY_TOKEN": "a relay token with spaces #1",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "a relay token with spaces #1" not in response.text
    values = dotenv_values(env_file)
    assert values["WARMUP_MODEL"] == "claude-haiku-5-5"
    assert values["WARMUP_ENABLED"] == "false"
    assert values["MODEL_CONCURRENCY_LIMITS_JSON"] == '{"claude-opus-5-5":2}'
    assert values["EGRESS_RELAY_TOKEN"] == "a relay token with spaces #1"
    assert env_file.read_text(encoding="utf-8").startswith("# scratch\n")
    assert config.get_settings().WARMUP_MODEL == "claude-haiku-5-5"
    assert config.get_settings().WARMUP_ENABLED is False
    assert "LOG_LEVEL" in body["pending_restart"]
    assert "EGRESS_RELAY_TOKEN" in body["pending_restart"]
    assert "WARMUP_MODEL" not in body["pending_restart"]
    assert body["pending_redeploy"] == ["HTTP_PORT"]
    assert body["pending_relay"] == ["EGRESS_RELAY_TOKEN"]
    assert body["relay_restart_command"] == "docker compose -f docker-compose.egress.yml up -d"
    assert _entry(body, "EGRESS_RELAY_TOKEN")["relay_restart"] is True
    assert OWNER_PASSWORD not in response.text


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DB_POOL_SIZE", "abc"),
        ("DB_POOL_SIZE", "0"),
        ("WARMUP_TRIGGER_POOL_USAGE_PCT", "1.5"),
        ("WARMUP_ENABLED", "maybe"),
        ("LOG_LEVEL", "LOUD"),
        ("MODEL_CONCURRENCY_LIMITS_JSON", "{not json"),
        ("MODEL_CONCURRENCY_LIMITS_JSON", "[1, 2]"),
        ("EGRESS_TARGETS_JSON", '[{"id": "x"}]'),
        ("THINKING_LEVELS", "low,turbo"),
        ("ADMIN_PASSWORD", "short"),
        ("ADMIN_PASSWORD", ""),
        ("HTTP_PORT", "70000"),
        ("WARMUP_MODEL", "line\nbreak"),
        ("NOT_A_SETTING", "1"),
        # Compose / Traefik values, checked like the installer's CCP_* overrides.
        ("HTTP_PORT", "+8080"),
        ("HTTPS_PORT", "0"),
        ("EGRESS_RELAY_HEALTH_PORT", "065536"),
        ("HTTP_BIND_ADDRESS", "999.0.0.1"),
        ("HTTP_BIND_ADDRESS", "10.0.0.1:80"),
        ("HTTP_BIND_ADDRESS", "localhost"),
        ("DOMAIN", ""),
        ("DOMAIN", "evil.com`) || Host(`other"),
        ("DOMAIN", "has space.example.com"),
        ("DOMAIN", "https://proxy.example.com"),
        ("DOMAIN", "proxy.example.com/path"),
        ("DOMAIN", "-leading.example.com"),
        ("TRAEFIK_ENTRYPOINT", "traefik"),
        ("ACME_EMAIL", "not an email"),
        ("ACME_EMAIL", "ops@localhost"),
        ("ACME_EMAIL", "ops@example.com`)"),
        ("ARCHIVE_BORG_HOST_DIR", "relative/borg"),
        ("ARCHIVE_BORG_HOST_DIR", "/"),
        ("ARCHIVE_BORG_HOST_DIR", "/etc/borg"),
        ("ARCHIVE_BORG_HOST_DIR", "/srv/../etc/borg"),
        ("ARCHIVE_BORG_HOST_DIR", "/srv//borg"),
        ("ARCHIVE_BORG_HOST_DIR", "//srv/borg"),
        ("ARCHIVE_BORG_HOST_DIR", "/srv/borg/"),
        ("ARCHIVE_BORG_HOST_DIR", "/lib64/borg"),
        ("ARCHIVE_BORG_HOST_DIR", "/var/run/docker"),
        ("ARCHIVE_BORG_HOST_DIR", "/proc"),
    ],
)
def test_put_rejects_invalid_values_per_key(client, admin_headers, env_file, key, value):
    before = env_file.read_text(encoding="utf-8")
    response = _put(client, admin_headers, {key: value, "WARMUP_MODEL": "ok"} if key != "WARMUP_MODEL" else {key: value})
    assert response.status_code == 422, response.text
    locations = [item["loc"][-1] for item in response.json()["detail"]]
    assert key in locations
    assert env_file.read_text(encoding="utf-8") == before


def test_put_rejects_read_only_keys(client, admin_headers, env_file):
    for key in ("FERNET_KEY", "JWT_SECRET", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB"):
        response = _put(client, admin_headers, {key: "x" * 48})
        assert response.status_code == 422, response.text
        assert "read-only" in response.json()["detail"][0]["msg"]
    assert env_file.read_text(encoding="utf-8") == "# scratch\n"


def test_put_accepts_valid_compose_values(client, admin_headers, env_file):
    values = {
        "HTTP_PORT": "80",
        "HTTPS_PORT": 443,
        "EGRESS_RELAY_HEALTH_PORT": "18080",
        "HTTP_BIND_ADDRESS": "10.8.0.1",
        "DOMAIN": "proxy.example.com",
        "TRAEFIK_ENTRYPOINT": "websecure",
        "ACME_EMAIL": "ops+acme@example.com",
        "ARCHIVE_BORG_HOST_DIR": "/srv/claude-code-proxy/borg",
    }
    response = _put(client, admin_headers, values)
    assert response.status_code == 200, response.text
    written = dotenv_values(env_file)
    assert written["HTTPS_PORT"] == "443"
    assert written["DOMAIN"] == "proxy.example.com"
    # Optional Compose values can be blanked (Compose then uses its ${VAR:-default}).
    response = _put(client, admin_headers, {"HTTP_BIND_ADDRESS": "", "ACME_EMAIL": ""}, password=None)
    assert response.status_code == 200, response.text
    assert dotenv_values(env_file)["HTTP_BIND_ADDRESS"] == ""


# --- owner password for sensitive changes -------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("EGRESS_RELAY_USERNAME", "relay-user"),
        ("DB_POOL_SIZE", "6"),
        ("JWT_EXPIRE_MINUTES", "30"),
        ("ADMIN_USERNAME", "owner"),
        ("ADMIN_PASSWORD", "a-new-owner-password"),
        ("ARCHIVE_S3_SECRET_ACCESS_KEY", "s3-secret"),
    ],
)
def test_sensitive_changes_need_the_current_owner_password(client, admin_headers, env_file, caplog, key, value):
    caplog.set_level(logging.DEBUG)
    for password in (None, "", "wrong-password-123", OWNER_PASSWORD + "x"):
        response = _put(client, admin_headers, {key: value}, password=password)
        assert response.status_code == 403, response.text
        assert env_file.read_text(encoding="utf-8") == "# scratch\n"
    missing = _put(client, admin_headers, {key: value}, password=None)
    assert key in missing.json()["detail"]
    assert "wrong-password-123" not in caplog.text

    response = _put(client, admin_headers, {key: value})
    assert response.status_code == 200, response.text
    assert dotenv_values(env_file)[key] == value
    assert OWNER_PASSWORD not in caplog.text


def test_live_and_unchanged_values_need_no_password(client, admin_headers, env_file):
    env_file.write_text("DB_POOL_SIZE=5\n", encoding="utf-8")
    _bump(env_file)
    response = _put(client, admin_headers, {"WARMUP_MODEL": "claude-haiku-5-5", "DB_POOL_SIZE": 5, "DOMAIN": "proxy.example.com"}, password=None)
    assert response.status_code == 200, response.text
    assert _entry(response.json(), "DB_POOL_SIZE")["requires_password"] is True
    assert _entry(response.json(), "WARMUP_MODEL")["requires_password"] is False
    assert _entry(response.json(), "FERNET_KEY")["requires_password"] is False  # read-only anyway


# --- clearing optional secrets -------------------------------------------------


def test_optional_secrets_can_be_cleared_required_ones_cannot(client, admin_headers, env_file):
    env_file.write_text(
        "EGRESS_RELAY_TOKEN=relay-token-value-that-is-long\nARCHIVE_S3_ACCESS_KEY_ID=AKIAEXAMPLE\nARCHIVE_S3_SECRET_ACCESS_KEY=s3secret\n",
        encoding="utf-8",
    )
    _bump(env_file)
    state = client.get("/api/v1/settings", headers=admin_headers).json()
    assert _entry(state, "EGRESS_RELAY_TOKEN")["clearable"] is True
    assert _entry(state, "ADMIN_PASSWORD")["clearable"] is False

    response = _put(client, admin_headers, {"EGRESS_RELAY_TOKEN": "", "ARCHIVE_S3_ACCESS_KEY_ID": "", "ARCHIVE_S3_SECRET_ACCESS_KEY": ""})
    assert response.status_code == 200, response.text
    values = dotenv_values(env_file)
    assert values["EGRESS_RELAY_TOKEN"] == values["ARCHIVE_S3_ACCESS_KEY_ID"] == values["ARCHIVE_S3_SECRET_ACCESS_KEY"] == ""
    assert _entry(response.json(), "EGRESS_RELAY_TOKEN")["is_set"] is False
    assert not config.get_settings().EGRESS_RELAY_TOKEN

    response = _put(client, admin_headers, {"ADMIN_PASSWORD": ""})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "ADMIN_PASSWORD"


# --- keys the registry does not know ------------------------------------------


def test_unregistered_file_keys_are_listed_read_only_in_other(client, admin_headers, env_file):
    env_file.write_text("COMPOSE_PROJECT_NAME=ccp\nMY_SERVICE_API_KEY=hidden-api-key-value-789\nBACKUP_PASS=hidden-pass-456\n", encoding="utf-8")
    _bump(env_file)
    response = client.get("/api/v1/settings", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["groups"][-1] == "Other"
    assert "hidden-api-key-value-789" not in response.text
    assert "hidden-pass-456" not in response.text
    project = _entry(body, "COMPOSE_PROJECT_NAME")
    assert project["group"] == "Other" and project["read_only"] is True and project["value"] == "ccp"
    assert project["type"] == "string" and project["secret"] is False
    for key in ("MY_SERVICE_API_KEY", "BACKUP_PASS"):
        entry = _entry(body, key)
        assert entry["secret"] is True and entry["value"] is None and entry["is_set"] is True and entry["read_only"] is True

    response = _put(client, admin_headers, {"COMPOSE_PROJECT_NAME": "other"})
    assert response.status_code == 422
    assert "Not managed" in response.json()["detail"][0]["msg"]


def test_other_group_is_absent_without_unregistered_keys(client, admin_headers, env_file):
    body = client.get("/api/v1/settings", headers=admin_headers).json()
    assert "Other" not in body["groups"]


# --- unreadable config file ----------------------------------------------------


def test_unreadable_file_at_start_uses_the_environment(client, admin_headers, env_file, monkeypatch, caplog):
    monkeypatch.setenv("WARMUP_MODEL", "from-environment")
    env_file.write_text("WARMUP_MODEL=from-file\n", encoding="utf-8")
    _unreadable(monkeypatch)
    config.get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="app.config"):
        assert config.get_settings().WARMUP_MODEL == "from-environment"
    assert "not readable" in caplog.text

    body = client.get("/api/v1/settings", headers=admin_headers).json()
    assert body["readable"] is False and body["writable"] is False
    response = _put(client, admin_headers, {"WARMUP_MODEL": "x"})
    assert response.status_code == 409
    assert env_file.read_text(encoding="utf-8") == "WARMUP_MODEL=from-file\n"

    monkeypatch.undo()  # readable again (undo also restores the fixture's settings, so re-point them)
    monkeypatch.setenv("CCP_CONFIG_FILE", str(env_file))
    monkeypatch.setattr(config, "RELOAD_CHECK_SECONDS", 0.0)
    assert config.get_settings().WARMUP_MODEL == "from-file"


def test_file_becoming_unreadable_keeps_current_settings_and_sidecars(env_file, monkeypatch):
    monkeypatch.setattr(config_watch, "CHECK_SECONDS", 0.0)
    env_file.write_text("WARMUP_MODEL=from-file\nDB_POOL_SIZE=5\n", encoding="utf-8")
    _bump(env_file)
    handle = config.get_settings()
    assert handle.WARMUP_MODEL == "from-file"
    raw = config_watch.RestartWatcher(use_settings=False, log=lambda _message: None)
    typed = config_watch.RestartWatcher(log=lambda _message: None)

    read_text = config_file.read_text
    _unreadable(monkeypatch)
    _bump(env_file)
    assert handle.WARMUP_MODEL == "from-file"
    assert raw.changed() == []
    raw.exit_if_changed()
    typed.exit_if_changed()

    # Readable again: a real change is still noticed.
    monkeypatch.setattr(config_file, "read_text", read_text)
    env_file.write_text("WARMUP_MODEL=back\nDB_POOL_SIZE=7\n", encoding="utf-8")
    _bump(env_file)
    assert handle.WARMUP_MODEL == "back"
    assert raw.changed() == ["DB_POOL_SIZE"]


def test_read_text_raises_for_unreadable_files(tmp_path):
    path = tmp_path / ".env"
    path.mkdir()  # reading a directory fails for every user, including root
    with pytest.raises(OSError):
        config_file.read_text(path)
    assert config_file.read_text(tmp_path / "missing") is None


def test_put_refuses_when_file_is_not_writable(client, admin_headers, env_file, monkeypatch):
    monkeypatch.setattr(config_file, "is_writable", lambda _path: False)
    response = _put(client, admin_headers, {"WARMUP_MODEL": "x"})
    assert response.status_code == 409
    assert env_file.read_text(encoding="utf-8") == "# scratch\n"


def test_put_reports_a_full_disk_and_leaves_the_file(client, admin_headers, env_file, monkeypatch):
    def full_disk(_descriptor, _offset, _length):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(config_file.os, "posix_fallocate", full_disk, raising=False)
    response = _put(client, admin_headers, {"WARMUP_MODEL": "x"})
    assert response.status_code == 507, response.text
    assert "left unchanged" in response.json()["detail"]
    assert env_file.read_text(encoding="utf-8") == "# scratch\n"


def test_restart_signals_only_a_gunicorn_master(client, admin_headers, env_file, monkeypatch):
    assert client.post("/api/v1/settings/restart", headers=admin_headers).status_code == 409

    sent = []
    monkeypatch.setattr(settings_route, "_gunicorn_master_pid", lambda: 4242)
    monkeypatch.setattr(settings_route.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    response = client.post("/api/v1/settings/restart", headers=admin_headers)
    assert response.status_code == 202, response.text
    assert sent == [(4242, signal.SIGHUP)]


def test_settings_are_owner_only(client, admin_headers, env_file):
    from app.utils.security import PERMISSIONS

    password = "member-password-123"
    created = client.post(
        "/api/v1/team/members",
        headers=admin_headers,
        json={"username": "almost-owner", "password": password, "permissions": sorted(PERMISSIONS)},
    )
    assert created.status_code == 201, created.text
    granted = client.post(
        "/api/v1/team/members",
        headers=admin_headers,
        json={"username": "sneaky", "password": password, "permissions": ["settings:read", "settings:write"]},
    )
    assert granted.status_code == 422  # not an assignable permission
    login = client.post("/api/v1/auth/login", json={"username": "almost-owner", "password": password})
    member = {"Authorization": f"Bearer {login.json()['token']}"}

    assert client.get("/api/v1/settings", headers=member).status_code == 403
    assert client.put("/api/v1/settings", headers=member, json={"values": {"WARMUP_MODEL": "x"}}).status_code == 403
    assert client.post("/api/v1/settings/restart", headers=member).status_code == 403
    assert client.get("/api/v1/settings").status_code == 401
    assert client.get("/api/v1/settings", headers=admin_headers).status_code == 200
    assert env_file.read_text(encoding="utf-8") == "# scratch\n"
