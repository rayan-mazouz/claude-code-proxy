# Path: app/utils/security/throttle.py
# Description: Sliding-window throttle for dashboard login attempts.

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Request


@dataclass(frozen=True)
class ThrottleRule:
    """At most ``max_failures`` failed attempts per key inside ``window_seconds``."""

    max_failures: int
    window_seconds: float


class FailureThrottle:
    """Thread-safe failure counter keyed by arbitrary strings.

    Failures are kept per key in a sliding window, and the number of tracked keys is bounded (least recently used
    keys are evicted first) so a flood of distinct usernames or addresses cannot grow memory without limit.

    State lives in the worker process. With N workers an attacker gets at most N times the configured budget,
    which still keeps online guessing several orders of magnitude below what a strong password requires.
    """

    def __init__(self, rule: ThrottleRule, *, max_keys: int = 10_000, clock: Callable[[], float] = time.monotonic) -> None:
        self._rule = rule
        self._max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> Optional[deque[float]]:
        failures = self._failures.get(key)
        if failures is None:
            return None
        cutoff = now - self._rule.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return None
        return failures

    def retry_after(self, key: str) -> Optional[int]:
        """Seconds until ``key`` may try again, or None when it is not throttled."""
        now = self._clock()
        with self._lock:
            failures = self._prune(key, now)
            if failures is None or len(failures) < self._rule.max_failures:
                return None
            oldest_counted = failures[len(failures) - self._rule.max_failures]
            return max(1, int(oldest_counted + self._rule.window_seconds - now + 0.999))

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            failures = self._prune(key, now)
            if failures is None:
                failures = deque()
                self._failures[key] = failures
            failures.append(now)
            # Never keep more timestamps than the rule can use.
            while len(failures) > self._rule.max_failures:
                failures.popleft()
            self._failures.move_to_end(key)
            while len(self._failures) > self._max_keys:
                self._failures.popitem(last=False)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()


# Limits are sized for deployments where every request appears to come from the same address (rootless Docker
# port forwarding and some reverse proxies hide client IPs). The per-account limit is what stops password guessing:
# 20 attempts per 15 minutes is under 2,000 a day, hopeless against the generated owner password or a 12+ character
# member password. Per-source limits add protection when real client addresses are visible, and are generous
# enough that a shared address does not turn into a global lockout.
_WINDOW_SECONDS = 15 * 60
_per_source_and_user = FailureThrottle(ThrottleRule(max_failures=10, window_seconds=_WINDOW_SECONDS))
_per_user = FailureThrottle(ThrottleRule(max_failures=20, window_seconds=_WINDOW_SECONDS))
_per_source = FailureThrottle(ThrottleRule(max_failures=100, window_seconds=_WINDOW_SECONDS))


def client_address(request: Request) -> str:
    """Best-effort client address for throttling.

    The backend is only reachable through the bundled Traefik, which discards client-supplied X-Forwarded-For
    headers and records the peer it accepted the connection from, so the first entry identifies the caller.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",", 1)[0].strip()
    if first:
        return first[:64]
    return request.client.host if request.client else "unknown"


def _keys(source: str, username: str) -> tuple[tuple[FailureThrottle, str], ...]:
    user = username.strip().lower()[:200]
    return (
        (_per_source_and_user, f"{source}\x00{user}"),
        (_per_source, source),
        (_per_user, user),
    )


def login_retry_after(source: str, username: str) -> Optional[int]:
    """Seconds the caller must wait before another login attempt, or None when allowed."""
    waits = [wait for throttle, key in _keys(source, username) if (wait := throttle.retry_after(key)) is not None]
    return max(waits) if waits else None


def record_login_failure(source: str, username: str) -> None:
    for throttle, key in _keys(source, username):
        throttle.record_failure(key)


def record_login_success(source: str, username: str) -> None:
    # Only the pair counter resets: a successful login must not erase evidence of guessing from elsewhere.
    throttle, key = _keys(source, username)[0]
    throttle.reset(key)


def reset_login_throttle() -> None:
    """Forget every recorded failure (used by tests)."""
    for throttle in (_per_source_and_user, _per_source, _per_user):
        throttle.clear()
