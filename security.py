"""Rate limiting, brute-force lockout, and spend accounting.

All in-process and lock-guarded: this app is a single uvicorn process by design.
Running multiple workers means moving these three structures to Redis — the
interfaces here are deliberately the ones a Redis implementation would expose.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from config import settings


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------
@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Classic token bucket, keyed by whatever string you pass in.

    Request-count limiting alone is weak for an LLM app — one request can cost
    100x another — so this runs alongside the USD ceiling in `SpendLedger`, not
    instead of it.
    """

    def __init__(self, rate_per_min: float, burst: int) -> None:
        self.rate = rate_per_min / 60.0
        self.burst = float(burst)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                self._buckets[key] = _Bucket(self.burst - 1.0, now)
                return True, 0.0
            b.tokens = min(self.burst, b.tokens + (now - b.updated) * self.rate)
            b.updated = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0
            return False, (1.0 - b.tokens) / self.rate if self.rate else 60.0

    def prune(self, older_than: float = 3600.0) -> None:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, b in self._buckets.items() if now - b.updated > older_than]:
                del self._buckets[k]


chat_limiter = RateLimiter(settings.chat_rate_per_min, settings.chat_burst)
sql_limiter = RateLimiter(settings.sql_rate_per_min, settings.sql_burst)
verify_limiter = RateLimiter(settings.verify_rate_per_min, settings.verify_burst)


# ---------------------------------------------------------------------------
# Brute-force lockout
# ---------------------------------------------------------------------------
@dataclass
class _Failures:
    count: int = 0
    locked_until: float = 0.0
    first_seen: float = field(default_factory=time.monotonic)


class Lockout:
    """Exponential backoff per client after repeated bad passwords.

    `hmac.compare_digest` stops the timing attack; this stops the online guessing
    attack, which is the one that actually matters against a human-chosen password.
    """

    def __init__(self) -> None:
        self._f: dict[str, _Failures] = defaultdict(_Failures)
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> float:
        with self._lock:
            f = self._f.get(key)
            if not f:
                return 0.0
            return max(0.0, f.locked_until - time.monotonic())

    def record_failure(self, key: str) -> float:
        with self._lock:
            f = self._f[key]
            f.count += 1
            if f.count >= settings.lockout_threshold:
                over = f.count - settings.lockout_threshold
                delay = min(
                    settings.lockout_max_seconds,
                    settings.lockout_base_seconds * (2**over),
                )
                f.locked_until = time.monotonic() + delay
                return delay
            return 0.0

    def record_success(self, key: str) -> None:
        with self._lock:
            self._f.pop(key, None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"tracked_clients": len(self._f),
                    "locked_now": sum(1 for f in self._f.values()
                                      if f.locked_until > time.monotonic())}


lockout = Lockout()


# ---------------------------------------------------------------------------
# Spend ledger
# ---------------------------------------------------------------------------
class SpendLedger:
    """Tracks USD spent on the *server's* key, per UTC day.

    Requests paying with a caller-supplied key are recorded for observability but
    do not count against the ceiling — it is not our money.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day = self._today()
        self._usd = 0.0
        self._requests = 0
        self._tokens_in = 0
        self._tokens_out = 0

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._usd, self._requests = today, 0.0, 0
            self._tokens_in = self._tokens_out = 0

    def would_exceed(self) -> bool:
        with self._lock:
            self._roll()
            return self._usd >= settings.daily_usd_ceiling

    def record(self, usd: float, tokens_in: int, tokens_out: int, own_key: bool) -> None:
        with self._lock:
            self._roll()
            self._requests += 1
            self._tokens_in += tokens_in
            self._tokens_out += tokens_out
            if own_key:
                self._usd += usd

    def snapshot(self) -> dict[str, float | int | str]:
        with self._lock:
            self._roll()
            return {
                "day": self._day,
                "usd_spent_server_key": round(self._usd, 4),
                "usd_ceiling": settings.daily_usd_ceiling,
                "requests": self._requests,
                "tokens_in": self._tokens_in,
                "tokens_out": self._tokens_out,
            }


ledger = SpendLedger()


# ---------------------------------------------------------------------------
# Client identity for limiting: never the raw key, only a short digest.
# ---------------------------------------------------------------------------
def client_key(ip: str, credential: str | None) -> str:
    import hashlib

    if credential:
        return "k:" + hashlib.sha256(credential.encode()).hexdigest()[:16]
    return "ip:" + ip
