#!/usr/bin/env bash
# Dev launcher: fetch what git does not carry, then serve.
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt

if [ ! -d data/csv ] || [ -z "$(ls -A data/csv 2>/dev/null)" ]; then
  echo "==> fetching PokeAPI CSVs (~40 MB)"
  rm -rf _src
  git clone --depth 1 --filter=blob:none --sparse \
      https://github.com/PokeAPI/pokeapi.git _src
  git -C _src sparse-checkout set data/v2/csv
  mkdir -p data/csv && mv _src/data/v2/csv/*.csv data/csv/ && rm -rf _src
fi

if [ ! -d assets/sprites ]; then
  echo "==> fetching sprites (~754 MB, this takes a few minutes)"
  rm -rf _spr
  git clone --depth 1 --filter=blob:none --sparse \
      https://github.com/PokeAPI/sprites.git _spr
  git -C _spr sparse-checkout set \
      sprites/pokemon/other/official-artwork sprites/pokemon/other/home sprites/items
  mkdir -p assets && mv _spr/sprites assets/sprites && rm -rf _spr
fi

[ -f data/pokedex.sqlite ] || ./.venv/bin/python build_db.py

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> created .env — add your OPENROUTER_API_KEY, then re-run"
  exit 1
fi

exec ./.venv/bin/uvicorn server:app \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" \
  --timeout-graceful-shutdown 25
