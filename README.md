# Pokedex Chat

A grounded Pokemon analyst. **Every number comes from a local SQLite database**
queried with SQL by the model; **strategy and metagame come from live web search**.
The model is never allowed to recall a stat from memory or do arithmetic in its head.

```bash
git clone <this repo> && cd pokedex-chat
cp .env.example .env                       # add your OpenRouter key
python scripts/set_password.py --random    # set a password (or skip: the key stays locked)
./run.sh                                   # fetches data on first run, then serves :8000
```

---

## Contents

- [What's in the box](#whats-in-the-box)
- [How the grounding works](#how-the-grounding-works)
- [The UI](#the-ui)
- [Chat history](#chat-history)
- [Models](#models)
- [Who pays: API access](#who-pays-api-access)
- [Changing the password](#changing-the-password)
- [Cost controls](#cost-controls)
- [Observability](#observability)
- [Security posture](#security-posture)
- [Configuration](#configuration)
- [Getting the data](#getting-the-data)
- [Tests, CI and the prompt eval](#tests-ci-and-the-prompt-eval)
- [Deployment](#deployment)

---

## What's in the box

| Path | What |
|---|---|
| `data/pokedex.sqlite` | 80 MB. 143 tables + 17 views + 2 FTS5 indexes (built, not committed) |
| `assets/sprites/` | 754 MB of artwork + item icons, all local (fetched, not committed) |
| `build_db.py` | CSV → SQLite: views, indexes, full-text |
| `pokedb.py` | Read-only query layer, sqlglot guard, sprite resolution, the schema doc |
| `agent.py` | Tool-calling loop, prompt, model shortlist, prompt caching, cancellation |
| `server.py` | FastAPI: routes, auth, rate limits, CSP, lifespan |
| `config.py` | Settings + the hot-reloading secret store |
| `security.py` | Token bucket, brute-force lockout, spend ledger |
| `observability.py` | JSON logging, request ids, cost accounting |
| `static/` | The UI. Vanilla JS, no build step, every library vendored |
| `tests/` | 100 tests; the provider is mocked, so no test spends a token |
| `evals/` | Golden dataset that gates prompt changes |

### Data coverage

Complete through **generation 9**, including the 2025–26 wave:
1 025 species / **1 351 forms** — 97 Mega Evolutions (Legends Z-A included),
34 Gigantamax forms, primals, all four regional families, totems;
937 moves with power/accuracy/PP/priority/flags/effects, Z-moves and Max moves
flagged; 2 223 items; 373 abilities; 14 496 English Pokedex entries;
638 322 learnset rows across every version group; the full 18×18 type chart;
every evolution trigger with its exact condition columns.

Nothing is fetched from a Pokemon API at runtime.

---

## How the grounding works

Four tools:

- **`sql_query`** — one read-only `SELECT`/`WITH`. The statement is **parsed** with
  sqlglot, not regex-matched, so `WHERE entry LIKE '%create%'` works while
  `WITH t AS (…) DELETE …` does not. Runs off the event loop, 8 s interrupt,
  400-row cap. Read-only is enforced at the connection (`mode=ro` +
  `PRAGMA query_only`) — the parser is defence in depth.
- **`web_search`** — OpenRouter's web plugin. Results are fenced in
  `<untrusted_web_content>`, markdown images and link targets are stripped, and
  the prompt restates that tool output is data and never instructions.
- **`get_sprite`** / **`get_item_sprite`** — name or form key → local PNG.

The system prompt carries the schema documentation, a map of *where each special
mechanic lives* in the data, query recipes for the awkward ones, a trust boundary,
and an explicit "say you cannot answer rather than guess" clause. It is versioned
(`agent.PROMPT_VERSION`) and gated by an eval.

Every tool call is shown in the UI: click a row to see the exact SQL and the rows
it returned, or the sources a search used.

```bash
curl -s localhost:8000/api/sql -H 'content-type: application/json' \
  -H 'X-App-Password: …' \
  -d '{"sql":"SELECT species_name, bst FROM v_pokemon WHERE is_default_form=1 ORDER BY bst DESC LIMIT 5"}'
```

`GET /api/schema` returns the same documentation the model sees.

---

## The UI

Minimal, dark by default; the toggle gives a slightly grey off-white. Both themes
are selected, not flipped.

**Streaming markdown** is the part that is easy to get wrong, so it is deliberate:

1. **Debounced re-render (~60 ms)**, not per token.
2. **Tail repair before parsing** — an unclosed fence, `**`, backtick, `$math$` or
   half-typed `[link](` is closed optimistically, so text never flickers between
   raw and formatted.
3. **Block-level diffing** — `marked.lexer` splits the message into blocks, each
   with its own DOM node; blocks `0..N-1` are byte-identical each tick so only the
   last re-renders. Streamed text stays selectable.
4. **DOMPurify per block** — and if it *removes* anything, rendering stops with a
   warning rather than being patched up.
5. **Deferred heavy blocks** — ` ```mermaid ` and ` ```chart ` show a placeholder
   until the closing fence arrives.

Renders GFM tables (numeric columns auto right-aligned), highlight.js, KaTeX,
Mermaid, local sprites, and a dependency-free SVG chart renderer (`bar`, `hbar`,
`line`, `radar`). A malformed chart block degrades to a table of the same numbers
rather than an error message.

Type `/demo` to stream a canned answer exercising every renderer, free.

**Stop** cancels mid-answer: the button (or `Esc`) aborts the request, the server
detects the disconnect and breaks the agent loop, and whatever streamed is kept.

---

## Chat history

Stored in the browser, in `localStorage`. **No database, no server state, nothing
leaves your machine.**

- Sidebar lists every conversation, newest first, with turn count and age.
- Titles come from the first message — no tokens spent naming things.
- **Export** downloads all conversations as JSON. **Delete all** wipes them.
- Bounded by the ~5 MB quota: when it fills, the oldest conversation is evicted
  rather than the write throwing and losing the one you are in.
- A conversation remembers the model it last used.

Trade-off: it is per-browser and per-device. That is the price of not running a
database.

---

## Models

The picker shows nine models that hold a multi-round tool loop together, cheapest
first, with in/out price per million tokens in the label:

| Model | in / out per Mtok | Why it's here |
|---|---|---|
| **DeepSeek V3.2** | $0.27 / $0.40 | **default** — cheapest that still writes good SQL |
| Gemini 3.1 Flash Lite | $0.25 / $1.50 | fastest cheap option, 1M context |
| GPT-5 mini | $0.25 / $2.00 | solid tool loops at budget price |
| Grok 4.3 | $1.25 / $2.50 | cheap output — long answers stay affordable |
| Claude Haiku 4.5 | $1.00 / $5.00 | quick, careful with numbers |
| GPT-5.1 | $1.25 / $10.00 | strong multi-step reasoning |
| Claude Sonnet 5 | $2.00 / $10.00 | best all-round |
| Gemini 3.1 Pro | $2.00 / $12.00 | wide context, long comparisons |
| Claude Opus 5 | $5.00 / $25.00 | genuinely hard analysis |

Sorted by **output** price: the question is short and the schema prompt is fixed,
so the bill tracks how much the model *writes* across tool rounds.

**Switching mid-conversation is allowed and applies from your next message.**
Earlier turns keep the answers they were given — the switch changes what answers
the next question, not what already happened. Cheap model for lookups, expensive
one for the hard follow-up, same thread.

Prices and context are read live from OpenRouter and cached for 10 minutes; only
ids and one-line notes are pinned, in `CURATED_MODELS` at the top of `agent.py`.
The last entry, **all models on OpenRouter…**, swaps to the full catalogue.

---

## Who pays: API access

The **gear icon** opens *API access*. The server picks its mode from `.env` and
logs it at startup.

**The key in `.env` is not spendable by default.** Something has to unlock it —
forgetting to set a password locks the key rather than sharing it.

| Mode | When | What a visitor must do |
|---|---|---|
| `password` | key **and** `APP_PASSWORD` set | Type the password — or bring their own key |
| `locked` | key set, `APP_PASSWORD` empty | Bring their own key; yours is refused, for everyone |
| `byok` | no key in `.env` | Bring their own key |
| `open` | key set **and** `ALLOW_OPEN=true` | Nothing — opt-in only |

Anyone can pick **My own key** and paste an OpenRouter key. It is verified against
`/api/v1/key` (which actually authenticates — `/models` is public and a fake key
passes it), kept in that browser's `localStorage`, sent as `X-Api-Key`, used for
that one request, and **never written to disk, logged, or cached** server-side.
A caller-supplied key always beats the server's and is exempt from the daily
budget — it is not your money.

---

## Changing the password

`.env` is re-read whenever its mtime changes, so **a password change takes effect
on the next request. No restart, no redeploy, no cache to bust.** Every browser is
re-checked against the new value on its next message, because the password is
verified per request rather than exchanged for a session token.

```bash
python scripts/set_password.py            # prompt, store as an scrypt hash
python scripts/set_password.py --random   # generate a strong one, print it once
python scripts/set_password.py --plain    # store as plain text (laptop only)
python scripts/set_password.py --clear    # lock the key entirely
```

`APP_PASSWORD` accepts either form:

- **plain text** — fine for a laptop
- **`scrypt$<salt>$<key>`** — for anything shared; the password is not recoverable
  from `.env`

Both are compared in constant time. Revoking access is the same one-liner: run
`--random`, hand out the new password, and everyone with the old one is locked out
on their next message.

`ALLOW_OPEN` is live too, so open access can be revoked the same way.

An explicit environment variable still wins over the file, so container secrets and
`APP_PASSWORD=… uvicorn …` keep working.

---

## Cost controls

Request-count limiting alone is weak here — one question can cost 100× another —
so there are three layers:

| Control | Default | Where |
|---|---|---|
| Token bucket per credential and per IP | 12 chat/min, burst 6 | `security.RateLimiter` |
| Per-request USD ceiling | $0.75 | aborts the loop mid-answer |
| Daily USD ceiling on the **server's** key | $5.00 | `security.SpendLedger`, per UTC day |
| `max_tokens` per completion | 4 000 | every provider call |
| Conversation token ceiling | 250 000 | aborts a runaway loop |
| Tool-result cap | 24 000 chars | per call, fenced |
| Tool rounds | 14 | hard stop |

**Prompt caching** is on: the ~4k-token system prompt carries an Anthropic
`cache_control` breakpoint, and `db_stats()` is memoised so the prefix stays
byte-identical — a prompt that changes per request never hits any provider's cache.

Brute force is throttled by exponential backoff per IP: 5 failures, then 2 s
doubling to a 15-minute cap, with every failure logged.

---

## Observability

One JSON line per event, with a request id threaded through:

```json
{"ts":"2026-07-27T15:41:02.184Z","level":"INFO","logger":"pokedex","msg":"chat.completed",
 "request_id":"8861b9b07aa5","credential":"807201c5968e","own_key":true,
 "model":"anthropic/claude-haiku-4.5","rounds":2,"tool_calls":1,
 "tokens_in":4105,"tokens_out":180,"usd":0.005005,"ms":5364}
```

The credential is a SHA-256 prefix, never the key, so spend is attributable
without storing anything sensitive. `chat.started`, `tool.done`, `chat.cancelled`,
`auth.bad_password` and `ratelimit.blocked` are all recorded — which is what makes
abuse detectable at all.

`GET /api/diagnostics` (authenticated) returns live spend, lockout state, uptime,
prompt version and the secrets generation counter.

Set `LOG_FORMAT=text` for readable local output.

---

## Security posture

- **Fail-closed credentials** (above).
- **Every sensitive endpoint requires a credential** — `/api/chat`, `/api/sql`,
  `/api/models`, `/api/diagnostics`. `/api/health` is public and returns only
  `{"ok": true}`; it deliberately does not reveal the auth mode, because that tells
  an attacker which attack applies.
- **Roles constrained to `user`/`assistant`** (`422` otherwise), so a caller cannot
  post a second `system` message and override the grounding rules. Message count,
  message length, conversation size and SQL length are all capped.
- **SQL runs off the event loop.** Before this, one 8-second cross join made every
  other request wait 7 seconds.
- **Strict CSP** — `default-src 'self'; img-src 'self' data:` plus nosniff, DENY
  framing, `no-referrer`, COOP. Everything is same-origin, so this costs nothing
  and closes the markdown-image exfiltration path DOMPurify cannot see.
- **Untrusted web content is fenced and stripped** before the model reads it.
- **Upstream errors are never relayed.** Provider bodies echo account identifiers;
  they are logged with a request id and the user gets a generic message plus that
  id.
- **CORS is explicitly empty**, so nobody later "fixes" an error with
  `allow_origins=["*"]` and exposes everything.
- **All frontend libraries vendored** — no third-party script origin, which is what
  makes `localStorage` credential storage defensible. Versions and SRI hashes are
  recorded in `static/vendor/MANIFEST.md`.

**Still true:** the rate limiter, lockout table and spend ledger are in-process, so
this runs as a single worker. Scaling out means moving those three to Redis first —
their interfaces are already the ones a Redis version would expose.

---

## Configuration

`.env`, all optional except the key:

```
OPENROUTER_API_KEY=sk-or-v1-...     # the server's own key
APP_PASSWORD=                       # unlocks it. EMPTY = key locked
# ALLOW_OPEN=true                   # skip the password (solo localhost only)
GROQ_API_KEY=

# PROVIDER=openrouter               # openrouter | groq
# MODEL=deepseek/deepseek-v3.2      # pins the default; else auto-picked
# SEARCH_MODEL=openai/gpt-5-mini
# PUBLIC_URL=https://pokedex.example.com   # OpenRouter attribution
# DAILY_USD_CEILING=5.0
# PER_REQUEST_USD_CEILING=0.75
# CHAT_RATE_PER_MIN=12
# MAX_OUTPUT_TOKENS=4000
# LOG_FORMAT=json                   # json | text
# TRUST_PROXY=false                 # honour X-Forwarded-For behind a proxy
# CORS_ORIGINS=                     # comma-separated; empty = same-origin only
```

Settings are validated by pydantic at startup, so a bad value fails the boot
loudly instead of at the first request. Secrets are re-read live; everything else
is read once.

---

## Getting the data

`data/` and `assets/` are **not** in git — 794 MB of redistributable upstream
content. `./run.sh` fetches them on first run, or do it by hand:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/PokeAPI/pokeapi.git _src
git -C _src sparse-checkout set data/v2/csv
mkdir -p data/csv && mv _src/data/v2/csv/*.csv data/csv/ && rm -rf _src

git clone --depth 1 --filter=blob:none --sparse https://github.com/PokeAPI/sprites.git _spr
git -C _spr sparse-checkout set sprites/pokemon/other/official-artwork sprites/pokemon/other/home sprites/items
mkdir -p assets && mv _spr/sprites assets/sprites && rm -rf _spr

python build_db.py     # ~2 min
```

---

## Tests, CI and the prompt eval

```bash
pytest                      # 100 tests, provider mocked, ~3 s
python evals/run_eval.py    # offline: is the golden dataset still true?
ruff check . && mypy --ignore-missing-imports *.py
```

The suite covers the SQL guard (writes blocked, legitimate keyword-in-string
queries allowed), path traversal in sprite resolution, the four auth modes,
password hashing and **hot rotation**, lockout, rate limiting, the security
headers, request validation including the forged-`system`-role case, cost
arithmetic, and the streaming API with a mocked provider.

`evals/golden.yaml` is the regression gate on the prompt. Each case carries the
SQL that produced its expected answer, so the **offline** run re-derives the ground
truth from the database on every commit and fails if the dataset has rotted. The
**`--live`** run scores a real model and only runs when `agent.py` or the dataset
changes. Two cases exist purely to protect prompt clauses: one requires an
admission rather than a guess, one requires resisting an injected instruction.

CI runs secret scanning (gitleaks, with rules for OpenRouter/Groq key shapes),
ruff, mypy, the database build, the test suite, the offline eval, `pip-audit`
against the lockfile, and a Docker build. Dependencies are pinned in
`requirements.txt` and fully resolved in `requirements.lock`.

---

## Deployment

```bash
docker compose up -d      # app + Caddy for TLS
```

`Dockerfile` builds the database in a first stage and ships a slim runtime as a
non-root user. **Sprites are a mount, not a layer** — 754 MB in an image you
redeploy is 754 MB of waste; put them behind a CDN.

The reverse proxy is where TLS, the request-size limit and slow-loris protection
live; uvicorn provides none of them. The included `Caddyfile` also sets
`flush_interval -1`, without which SSE is buffered and the whole answer arrives at
once at the end.

Before exposing this beyond localhost:

1. Set `APP_PASSWORD`, never `ALLOW_OPEN`.
2. Terminate **TLS** — the password and any browser-supplied key travel as plain
   headers.
3. Set `TRUST_PROXY=true` so rate limiting keys on the real client IP rather than
   the proxy's.
4. Use a **spend-limited** OpenRouter key, and set `DAILY_USD_CEILING`.
5. Keep the frontend free of third-party scripts — that is what makes storing a
   credential in `localStorage` acceptable.

`audit.md` holds the full production-readiness review this hardening came from.
