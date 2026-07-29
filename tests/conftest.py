"""Shared fixtures. No test may ever reach OpenRouter."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point config at a scratch .env *before* importing anything that reads it.
_TEST_ENV = ROOT / "tests" / ".env.test"
os.environ["POKEDEX_ENV"] = str(_TEST_ENV)


@pytest.fixture(scope="session", autouse=True)
def _test_env():
    _TEST_ENV.write_text(
        "OPENROUTER_API_KEY=sk-or-v1-test-key-not-real\nAPP_PASSWORD=test-password-123\n",
        encoding="utf-8",
    )
    yield
    _TEST_ENV.unlink(missing_ok=True)


@pytest.fixture
def env_file() -> Path:
    return _TEST_ENV


@pytest.fixture(autouse=True)
def _reset_limits():
    """Rate limiters are process-global; give each test a clean slate."""
    import security

    for lim in (security.chat_limiter, security.sql_limiter, security.verify_limiter):
        lim._buckets.clear()
    security.lockout._f.clear()
    yield


@pytest.fixture
def client():
    """FastAPI test client with the lifespan running (so agent has an http client)."""
    from fastapi.testclient import TestClient

    import server

    with TestClient(server.app) as c:
        yield c
