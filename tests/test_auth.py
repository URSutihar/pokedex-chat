"""The credential model: the four auth modes, password handling, rotation."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import config
import server


@pytest.fixture
def clean_env(monkeypatch):
    """auth_mode() reads live values; make sure a stray shell var cannot leak in."""
    for k in ("APP_PASSWORD", "ALLOW_OPEN", "OPENROUTER_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield


def write_env(path, **pairs):
    path.write_text("\n".join(f"{k}={v}" for k, v in pairs.items()) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# auth_mode truth table
# --------------------------------------------------------------------------
def test_mode_byok_when_no_key(env_file, clean_env):
    write_env(env_file, APP_PASSWORD="irrelevant")
    assert server.auth_mode() == "byok"


def test_mode_password_when_key_and_password(env_file, clean_env):
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="pw")
    assert server.auth_mode() == "password"


def test_mode_locked_when_key_but_no_password(env_file, clean_env):
    """Fail closed: forgetting the password must lock the key, not share it."""
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="")
    assert server.auth_mode() == "locked"


def test_mode_open_only_when_explicitly_allowed(env_file, clean_env):
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="", ALLOW_OPEN="true")
    assert server.auth_mode() == "open"


# --------------------------------------------------------------------------
# resolve_key
# --------------------------------------------------------------------------
def test_caller_key_always_wins(env_file, clean_env):
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-server", APP_PASSWORD="pw")
    assert server.resolve_key("sk-or-v1-mine", None) == "sk-or-v1-mine"


def test_locked_mode_refuses_even_with_right_shape(env_file, clean_env):
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="")
    with pytest.raises(HTTPException) as e:
        server.resolve_key(None, "anything")
    assert e.value.status_code == 401


def test_password_unlocks_server_key(env_file, clean_env):
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="hunter2hunter2")
    assert server.resolve_key(None, "hunter2hunter2") is None


def test_wrong_password_rejected(env_file, clean_env):
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="hunter2hunter2")
    with pytest.raises(HTTPException) as e:
        server.resolve_key(None, "wrong", ip="1.2.3.4")
    assert e.value.status_code == 401


# --------------------------------------------------------------------------
# Password storage
# --------------------------------------------------------------------------
def test_scrypt_roundtrip():
    h = config.hash_password("correct horse battery staple")
    assert h.startswith("scrypt$")
    assert config.verify_password("correct horse battery staple", h)
    assert not config.verify_password("wrong", h)


def test_plaintext_still_supported():
    assert config.verify_password("abc", "abc")
    assert not config.verify_password("abd", "abc")


def test_empty_never_verifies():
    assert not config.verify_password("", "")
    assert not config.verify_password(None, "pw")
    assert not config.verify_password("pw", "")


def test_hashed_password_accepted_by_resolve_key(env_file, clean_env):
    write_env(
        env_file,
        OPENROUTER_API_KEY="sk-or-v1-x",
        APP_PASSWORD=config.hash_password("a-long-enough-password"),
    )
    assert server.resolve_key(None, "a-long-enough-password") is None
    with pytest.raises(HTTPException):
        server.resolve_key(None, "not-it", ip="9.9.9.9")


# --------------------------------------------------------------------------
# Hot rotation — the point of the Secrets class
# --------------------------------------------------------------------------
def test_password_change_takes_effect_without_restart(env_file, clean_env):
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="first-password")
    assert server.resolve_key(None, "first-password") is None

    # mtime resolution is coarse on some filesystems; the size differs here anyway
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="second-password-longer")

    with pytest.raises(HTTPException):
        server.resolve_key(None, "first-password", ip="5.5.5.5")
    assert server.resolve_key(None, "second-password-longer") is None


def test_explicit_environment_beats_the_file(env_file, clean_env, monkeypatch):
    """Container secrets and `APP_PASSWORD=x uvicorn …` must still win."""
    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="from-file")
    monkeypatch.setenv("APP_PASSWORD", "from-environment")
    assert config.app_password() == "from-environment"


# --------------------------------------------------------------------------
# Lockout
# --------------------------------------------------------------------------
def test_lockout_engages_after_threshold(env_file, clean_env):
    import security

    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="a-real-password")
    ip = "203.0.113.7"
    for _ in range(config.settings.lockout_threshold):
        with pytest.raises(HTTPException):
            server.resolve_key(None, "guess", ip=ip)
    assert security.lockout.locked_for(ip) > 0

    # even the correct password is refused while locked, with 429 not 401
    with pytest.raises(HTTPException) as e:
        server.resolve_key(None, "a-real-password", ip=ip)
    assert e.value.status_code == 429


def test_success_clears_failure_count(env_file, clean_env):
    import security

    write_env(env_file, OPENROUTER_API_KEY="sk-or-v1-x", APP_PASSWORD="a-real-password")
    ip = "203.0.113.8"
    with pytest.raises(HTTPException):
        server.resolve_key(None, "guess", ip=ip)
    server.resolve_key(None, "a-real-password", ip=ip)
    assert security.lockout.locked_for(ip) == 0
