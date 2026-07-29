"""Structured logging, request ids, and per-request cost accounting.

The app's central design question is "who pays", so the answer to "how much did
we spend, on what, for whom" has to be reconstructable from the log alone. Every
chat request emits one `chat.completed` record with model, tool rounds, tokens,
computed USD, latency and a *hashed* credential fingerprint — never the key.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any

from config import settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<5} [{request_id_var.get()}] {record.getMessage()}"
        extra = getattr(record, "fields", None)
        if extra:
            base += "  " + " ".join(f"{k}={v}" for k, v in extra.items())
        return base


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger("pokedex")


def emit(level: int, msg: str, **fields: Any) -> None:
    log.log(level, msg, extra={"fields": fields})


def info(msg: str, **f: Any) -> None:
    emit(logging.INFO, msg, **f)


def warn(msg: str, **f: Any) -> None:
    emit(logging.WARNING, msg, **f)


def error(msg: str, **f: Any) -> None:
    emit(logging.ERROR, msg, **f)


def new_request_id() -> str:
    rid = uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    return rid


def fingerprint(credential: str | None) -> str:
    """Stable, non-reversible label so spend can be attributed per credential."""
    import hashlib

    if not credential:
        return "anon"
    return hashlib.sha256(credential.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
class CostTracker:
    """Accumulates usage across the rounds of one agent loop."""

    def __init__(self, model: str, price_in: float = 0.0, price_out: float = 0.0) -> None:
        self.model = model
        self.price_in = price_in          # USD per million prompt tokens
        self.price_out = price_out
        self.tokens_in = 0
        self.tokens_out = 0
        self.rounds = 0
        self.tool_calls = 0
        self.started = time.perf_counter()

    def add_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.tokens_in += int(usage.get("prompt_tokens") or 0)
        self.tokens_out += int(usage.get("completion_tokens") or 0)

    @property
    def usd(self) -> float:
        return (self.tokens_in * self.price_in + self.tokens_out * self.price_out) / 1e6

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def as_fields(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usd": round(self.usd, 6),
            "ms": self.elapsed_ms,
        }
