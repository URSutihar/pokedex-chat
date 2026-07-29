"""Settings, and the hot-reloading secret store.

Two kinds of configuration live here:

* **Startup settings** — model, limits, ports. Read once, validated by pydantic.
* **Secrets** — the API keys and the app password. These are re-read from `.env`
  on every use, gated by the file's mtime, so changing the password takes effect
  on the next request with no restart and no redeploy. See `Secrets` below.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("POKEDEX_ENV", ROOT / ".env"))


class Settings(BaseSettings):
    """Startup configuration. Validated once; a bad value fails the boot loudly."""

    model_config = SettingsConfigDict(
        env_file=ENV_PATH, env_file_encoding="utf-8", extra="ignore"
    )

    provider: str = Field(default="openrouter", pattern="^(openrouter|groq)$")
    model: str = ""                       # pin the default; else auto-picked
    search_model: str = "openai/gpt-5-mini"
    port: int = 8000

    # attribution shown on OpenRouter's dashboard
    public_url: str = "http://localhost:8000"
    app_title: str = "Pokedex Chat"

    # cost ceilings -----------------------------------------------------------
    max_output_tokens: int = 4000
    max_tool_rounds: int = 14
    max_tool_result_chars: int = 24_000
    max_conversation_tokens: int = 250_000   # abort a runaway agent loop
    daily_usd_ceiling: float = 5.0           # server-key spend per UTC day
    per_request_usd_ceiling: float = 0.75

    # rate limits (token bucket) ----------------------------------------------
    chat_rate_per_min: float = 12.0
    chat_burst: int = 6
    sql_rate_per_min: float = 60.0
    sql_burst: int = 20
    # a human who typos twice should not be throttled; the lockout below is what
    # actually stops guessing
    verify_rate_per_min: float = 20.0
    verify_burst: int = 6

    # brute-force lockout on the password path --------------------------------
    lockout_threshold: int = 5          # failures before backoff starts
    lockout_base_seconds: float = 2.0   # doubles per failure past the threshold
    lockout_max_seconds: float = 900.0

    # misc --------------------------------------------------------------------
    allow_open: bool = False
    cors_origins: str = ""              # comma-separated; empty = same-origin only
    log_level: str = "INFO"
    log_format: str = Field(default="json", pattern="^(json|text)$")
    trust_proxy: bool = False           # honour X-Forwarded-For for client IP


settings = Settings()


# ---------------------------------------------------------------------------
# Secrets: re-read from disk, so they can change while the server runs.
# ---------------------------------------------------------------------------
def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


class Secrets:
    """Live view of the credential material in `.env`.

    Every read checks the file's mtime and size (cheap `stat`, ~1 µs) and reparses
    only when it changed. Consequence: editing `APP_PASSWORD` in `.env` is picked
    up by the very next request — no restart, no cache to bust, and every browser
    is re-authenticated against the new value at once, because the password is
    verified per request rather than exchanged for a session token.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._stamp: tuple[float, int] | None = None
        self._values: dict[str, str] = {}
        self._generation = 0

    def _refresh(self) -> None:
        try:
            st = self._path.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = (0.0, 0)
        if stamp == self._stamp:
            return
        with self._lock:
            if stamp == self._stamp:
                return
            self._values = _parse_env_file(self._path)
            self._stamp = stamp
            self._generation += 1

    def get(self, key: str, default: str = "") -> str:
        self._refresh()
        # an explicit environment variable always wins over the file, so
        # `APP_PASSWORD=x uvicorn ...` and container secrets still work
        env = os.environ.get(key)
        if env is not None and env.strip():
            return env.strip()
        return self._values.get(key, default).strip()

    @property
    def generation(self) -> int:
        """Increments whenever `.env` changes; useful for logging a rotation."""
        self._refresh()
        return self._generation


sec = Secrets(ENV_PATH)


# ---------------------------------------------------------------------------
# Password verification.
#
# Two accepted forms in APP_PASSWORD:
#   plain text          — convenient, fine for a laptop
#   scrypt$<salt>$<key> — hashed, for anything shared. Generate with
#                         `python scripts/set_password.py`
# Both are compared in constant time.
# ---------------------------------------------------------------------------
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2**15, 8, 1, 32
# OpenSSL caps scrypt memory at 32 MB by default, which N=2^15,r=8 exceeds
# (128*N*r ≈ 32 MB plus overhead). Raise the cap rather than weakening the work factor.
SCRYPT_MAXMEM = 128 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_LEN, maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(supplied: str | None, expected: str) -> bool:
    supplied = (supplied or "").strip()
    expected = (expected or "").strip()
    if not expected or not supplied:
        return False
    if expected.startswith("scrypt$"):
        try:
            _, salt_hex, key_hex = expected.split("$", 2)
            key = hashlib.scrypt(
                supplied.encode(), salt=bytes.fromhex(salt_hex),
                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN,
                maxmem=SCRYPT_MAXMEM,
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(key.hex(), key_hex)
    return hmac.compare_digest(supplied, expected)


def app_password() -> str:
    return sec.get("APP_PASSWORD")


def env_api_key(provider: str) -> str:
    return sec.get("OPENROUTER_API_KEY" if provider == "openrouter" else "GROQ_API_KEY")


def allow_open() -> bool:
    """Live, like the password — so you can revoke open access without a restart."""
    return sec.get("ALLOW_OPEN", str(settings.allow_open)).lower() in ("1", "true", "yes", "on")


# Startup timestamp, used by the readiness endpoint.
BOOT_TIME = time.time()
