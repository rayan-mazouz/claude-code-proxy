# Path: app/utils/config_watch.py
# Description: Lets sidecar loops exit cleanly when a restart-class setting changes, so Docker restarts them.

"""Restart-on-change for the sidecars.

The sidecars (quota refresher, notification dispatcher, archive retention) run under ``restart: unless-stopped``.
When a key whose value is captured at process start (database pool, logging, egress, archive client) changes in the
config file, the sidecar logs the key names and exits with status 0; Docker starts it again and the new process
reads the file, which outranks the container's stale environment.

A config file that becomes unreadable is never treated as a change: the watcher keeps waiting until it can read it.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Iterable, Optional

from app import config
from app.utils import config_file
from app.utils.config_registry import RESTART_KEYS

CHECK_SECONDS = 2.0


class RestartWatcher:
    def __init__(self, keys: Optional[Iterable[str]] = None, *, use_settings: bool = True, log: Optional[Callable[[str], None]] = None) -> None:
        """Watch ``keys`` (default: every restart-class key).

        ``use_settings`` compares validated Settings values (an invalid edit is ignored, as the live settings
        ignore it); otherwise the raw file values are compared, for processes that never build Settings.
        """
        self.keys = frozenset(keys) if keys is not None else RESTART_KEYS
        self.use_settings = use_settings
        self.log = log or (lambda message: print(message, flush=True))
        self.path = config.config_file_path()
        if use_settings:
            config.startup_settings()
            self.baseline: dict[str, Optional[str]] = {}
        else:
            snapshot = self._snapshot()
            # Unreadable at start: the process runs on the environment Compose copied from the same file.
            self.baseline = snapshot if snapshot is not None else {key: os.environ.get(key) for key in self.keys}
        self.signature = config_file.signature(self.path)
        self.checked_at = time.monotonic()

    def _snapshot(self) -> Optional[dict[str, Optional[str]]]:
        """Watched keys as the file has them, or None when the file cannot be read right now."""
        try:
            values = config_file.read_values(self.path)
        except (OSError, ValueError):
            return None
        return {key: values.get(key) for key in self.keys}

    def changed(self) -> list[str]:
        """Restart-class keys whose value differs from this process's startup value."""
        if self.use_settings:
            return config.pending_restart_keys(self.keys)
        now = time.monotonic()
        if now - self.checked_at < CHECK_SECONDS:
            return []
        self.checked_at = now
        signature = config_file.signature(self.path)
        if signature == self.signature:
            return []
        current = self._snapshot()
        if current is None:
            return []  # keep the old signature so the file is read again once it is readable
        self.signature = signature
        return sorted(key for key in self.keys if current.get(key) != self.baseline.get(key))

    def exit_if_changed(self) -> None:
        keys = self.changed()
        if keys:
            self.log(f"Configuration changed ({', '.join(keys)}); exiting so the container restarts with the new settings")
            raise SystemExit(0)

    def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds``, exiting early (via SystemExit) when a watched key changes."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.exit_if_changed()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(CHECK_SECONDS, remaining))
