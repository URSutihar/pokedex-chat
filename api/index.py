"""Vercel entrypoint.

Vercel's Python runtime looks for an ASGI `app` in this module and serves the
whole site through it — including `/static` and the sprite proxy — because the
one rewrite in vercel.json sends every path here.

Nothing about the application changes for serverless; the two hosting facts it
has to cope with are a read-only bundle (so the database travels gzipped and is
unpacked into /tmp on cold start, see pokedb._resolve_db) and no sprite tree on
disk (so SPRITE_SOURCE=proxy serves them from this same origin).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SPRITE_SOURCE", "proxy")
os.environ.setdefault("POKEDEX_DB_CACHE", "/tmp/pokedex-db")
# /tmp is the only writable path, and it is per-instance and ephemeral
os.environ.setdefault("SPRITE_CACHE_DIR", "/tmp/pokedex-sprites")

from server import app

__all__ = ["app"]
