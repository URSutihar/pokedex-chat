"""The sprite proxy. Its whole risk is becoming an open forward proxy."""
from __future__ import annotations

import pytest

import sprites


@pytest.mark.parametrize(
    "rel",
    [
        "pokemon/other/official-artwork/6.png",
        "pokemon/other/official-artwork/shiny/25.png",
        "pokemon/other/home/10325.png",
        "pokemon/other/home/shiny/1025.png",
        "items/life-orb.png",
        "items/master-ball.png",
    ],
)
def test_allows_real_sprite_paths(rel):
    assert sprites.is_allowed(rel)


@pytest.mark.parametrize(
    "rel",
    [
        # traversal
        "../../../etc/passwd",
        "pokemon/other/home/../../../../etc/passwd",
        "items/../../secret.png",
        # absolute / scheme injection — the classic SSRF shapes
        "https://attacker.example/x.png",
        "//attacker.example/x.png",
        "http://169.254.169.254/latest/meta-data/",
        "pokemon/other/home/6.png?x=https://attacker.example",
        "pokemon/other/home/6.png#https://attacker.example",
        # wrong directories
        "pokemon/versions/generation-i/red-blue/6.png",
        "secret/6.png",
        "pokemon/other/official-artwork/6.svg",
        # malformed ids
        "pokemon/other/home/abc.png",
        "pokemon/other/home/99999999.png",
        "pokemon/other/home/.png",
        "items/UPPERCASE.png",
        "items/semi;colon.png",
        "",
    ],
)
def test_rejects_everything_else(rel):
    assert not sprites.is_allowed(rel)


@pytest.mark.asyncio
async def test_fetch_refuses_disallowed_without_network(monkeypatch):
    """A rejected path must never reach the HTTP client at all."""

    class Boom:
        async def get(self, *a, **k):
            raise AssertionError("proxy attempted a request for a disallowed path")

    assert await sprites.fetch("https://attacker.example/x.png", Boom()) is None


@pytest.mark.asyncio
async def test_non_png_upstream_is_rejected(monkeypatch, tmp_path):
    """Upstream returning HTML (or anything not a PNG) must not be served on."""
    monkeypatch.setattr(sprites, "CACHE_DIR", tmp_path)

    class Resp:
        status_code = 200
        content = b"<html>not a png</html>"

    class Client:
        async def get(self, *a, **k):
            return Resp()

    assert await sprites.fetch("items/life-orb.png", Client()) is None


@pytest.mark.asyncio
async def test_oversize_upstream_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(sprites, "CACHE_DIR", tmp_path)

    class Resp:
        status_code = 200
        content = b"\x89PNG" + b"0" * (sprites.MAX_BYTES + 1)

    class Client:
        async def get(self, *a, **k):
            return Resp()

    assert await sprites.fetch("items/life-orb.png", Client()) is None


@pytest.mark.asyncio
async def test_fetch_caches_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(sprites, "CACHE_DIR", tmp_path)
    sprites._locks.clear()
    calls = []

    class Resp:
        status_code = 200
        content = b"\x89PNG" + b"payload"

    class Client:
        async def get(self, *a, **k):
            calls.append(a)
            return Resp()

    rel = "pokemon/other/home/6.png"
    first = await sprites.fetch(rel, Client())
    second = await sprites.fetch(rel, Client())
    assert first == second == b"\x89PNG" + b"payload"
    assert len(calls) == 1                      # second call served from cache
    assert (tmp_path / rel).is_file()
    assert not list(tmp_path.rglob("*.part"))   # no half-written files left behind


def test_upstream_host_is_pinned():
    """The host is not derived from anything a caller controls."""
    assert sprites.UPSTREAM.startswith("https://raw.githubusercontent.com/PokeAPI/sprites/")
