# Path: app/utils/config_file.py
# Description: Read and rewrite the operator's .env config file in place, keeping comments, blank lines and key order.

"""Low-level access to the editable ``.env`` config file.

The file is usually a single-file bind mount shared by several containers, so it is never replaced by an atomic
rename (that would leave every container looking at the old inode). Writers rewrite it in place under an exclusive
``fcntl`` lock; readers take a shared lock so they never see a half-written file.

Values are quoted so python-dotenv (the backend) and Docker Compose ``env_file`` (container creation) read exactly
the same string. Anything that cannot be written portably is rejected instead of being silently mangled.

This module deliberately imports nothing from ``app.config`` so scripts can use it without building ``Settings``.
"""

from __future__ import annotations

import errno
import fcntl
import io
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional

from dotenv import dotenv_values

_log = logging.getLogger(__name__)

# KEY=..., optionally prefixed by `export`, as python-dotenv and Compose accept it.
_KEY_LINE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*")
_VALID_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Characters that make an unquoted value parse differently (comments, interpolation, escapes).
_UNQUOTED_UNSAFE = set("#$\\")


class ConfigValueError(ValueError):
    """A value that cannot be written to the config file without changing its meaning."""


def format_value(value: str) -> str:
    """Return ``value`` as it should appear after ``KEY=``, quoted only when needed.

    Simple values (including JSON such as ``{"a":1}``) stay unquoted. Values with comment, interpolation or
    whitespace-edge characters are single-quoted (literal in both parsers), or double-quoted when they contain a
    single quote. Combinations neither quoting style can carry identically are refused.
    """
    if any(char in value for char in "\r\n\0"):
        raise ConfigValueError("Values cannot contain line breaks.")
    if "${" in value:
        # python-dotenv expands ${NAME} even inside single quotes, and Compose expands it in every other form.
        raise ConfigValueError("Values cannot contain ${ (it would be read as a variable reference).")
    if value == "":
        return ""
    if value == value.strip() and not (_UNQUOTED_UNSAFE & set(value)) and value[0] not in "'\"":
        return value
    if "'" not in value and "\\" not in value:
        return f"'{value}'"
    if '"' not in value and "\\" not in value and "$" not in value:
        return f'"{value}"'
    raise ConfigValueError("Value cannot be stored safely in .env: avoid backslashes, and do not combine ' with \" or $.")


def parse_text(text: Optional[str]) -> dict[str, str]:
    """Parse .env text exactly as the backend reads it (python-dotenv, same as pydantic-settings)."""
    if not text:
        return {}
    values = dotenv_values(stream=io.StringIO(text))
    return {key: value for key, value in values.items() if value is not None}


def _lock(handle, mode: int) -> bool:
    try:
        fcntl.flock(handle.fileno(), mode)
        return True
    except OSError:
        # Some filesystems (or exotic bind mounts) do not support flock; fall back to an unlocked read/write.
        return False


def read_text(path: Path) -> Optional[str]:
    """Return the file's text under a shared lock, or None when it does not exist.

    Any other failure (an unreadable mount, an I/O error) raises: an unreadable file is not an empty config.
    """
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            locked = _lock(handle, fcntl.LOCK_SH)
            try:
                return handle.read()
            finally:
                if locked:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return None


def read_values(path: Path) -> dict[str, str]:
    return parse_text(read_text(path))


def _closes(body: str, quote: str) -> bool:
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return True
        index += 1
    return False


def _entries(text: str) -> list[tuple[Optional[str], str, str]]:
    """Split the text into (key or None, `KEY=` prefix, raw chunk) entries; quoted multi-line values stay together."""
    lines = text.splitlines(keepends=True)
    entries: list[tuple[Optional[str], str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _KEY_LINE.match(line)
        if match is None:
            entries.append((None, "", line))
            index += 1
            continue
        chunk = line
        rest = line[match.end() :]
        if rest[:1] in ("'", '"'):
            quote, body = rest[0], rest[1:]
            while not _closes(body, quote) and index + 1 < len(lines):
                index += 1
                body += lines[index]
                chunk += lines[index]
        entries.append((match.group(1), match.group(0).rstrip(" \t"), chunk))
        index += 1
    return entries


def render(text: str, updates: Mapping[str, str]) -> str:
    """Return ``text`` with each updated key's line replaced (later duplicates dropped) and new keys appended."""
    formatted = {key: format_value(value) for key, value in updates.items()}
    for key in formatted:
        if not _VALID_KEY.match(key):
            raise ConfigValueError(f"Invalid key name: {key}")
    written: set[str] = set()
    parts: list[str] = []
    for key, prefix, chunk in _entries(text):
        if key is None or key not in formatted:
            parts.append(chunk)
            continue
        if key in written:
            continue
        newline = "\r\n" if chunk.endswith("\r\n") else "\n" if chunk.endswith("\n") else ""
        parts.append(f"{prefix}{formatted[key]}{newline}")
        written.add(key)
    result = "".join(parts)
    missing = [key for key in formatted if key not in written]
    if missing:
        if result and not result.endswith("\n"):
            result += "\n"
        result += "".join(f"{key}={formatted[key]}\n" for key in missing)
    return result


# fallocate errors meaning "this filesystem cannot reserve space", not "there is no space".
_FALLOCATE_UNSUPPORTED = {errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL, errno.ENOSYS}


def _reserve(descriptor: int, size: int, current_size: int) -> None:
    """Reserve ``size`` bytes before the file is truncated, so a full disk fails while the old text is intact.

    Unsupported filesystems are skipped silently. On a real failure (ENOSPC, EDQUOT, ...) any partial extension is
    undone and the error propagates, leaving the file exactly as it was.
    """
    fallocate = getattr(os, "posix_fallocate", None)
    if fallocate is None or size <= 0:
        return
    try:
        fallocate(descriptor, 0, size)
    except OSError as exc:
        if exc.errno in _FALLOCATE_UNSUPPORTED:
            return
        try:
            if os.fstat(descriptor).st_size > current_size:
                os.ftruncate(descriptor, current_size)
        except OSError:
            pass
        raise


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")


def _write_backup(path: Path, text: str) -> None:
    """Best effort: keep the previous text next to the file (mode 0600) when the directory is writable."""
    target = backup_path(path)
    if not os.access(path.parent, os.W_OK | os.X_OK):
        return
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
    except OSError as exc:
        _log.debug("Could not write config backup %s: %s", target, type(exc).__name__)


def write_values(path: Path, updates: Mapping[str, str]) -> bool:
    """Rewrite the file in place (truncate + write) under an exclusive lock. Returns True when content changed.

    The file keeps its inode, owner, group and mode, so a single-file bind mount keeps working in every container.
    The new size is reserved first (so a full disk fails before anything is truncated) and the previous text is
    copied to ``<name>.bak`` when the directory allows it.
    """
    for value in updates.values():
        format_value(value)  # validate before touching the file
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8", newline="") as handle:
        locked = _lock(handle, fcntl.LOCK_EX)
        try:
            current = handle.read()
            updated = render(current, updates)
            if updated == current:
                return False
            _reserve(handle.fileno(), len(updated.encode("utf-8")), len(current.encode("utf-8")))
            _write_backup(path, current)
            handle.seek(0)
            handle.write(updated)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            return True
        finally:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def is_writable(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    return os.access(path.parent, os.W_OK)


def signature(path: Path) -> tuple:
    """Cheap change detector: inode, mtime and size (None fields when the file is missing)."""
    try:
        stat = os.stat(path)
    except OSError:
        return (str(path), None, None, None)
    return (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)


def overlay_environ(path: Path, keys: Optional[Iterable[str]] = None) -> None:
    """Copy config-file values into ``os.environ`` so code reading the environment sees the file (file wins)."""
    wanted = set(keys) if keys is not None else None
    for key, value in read_values(path).items():
        if wanted is None or key in wanted:
            os.environ[key] = value
