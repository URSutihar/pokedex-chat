#!/usr/bin/env python3
"""Golden-dataset eval for the system prompt.

Two modes:

  offline (default)  Verifies that every `expect` value in golden.yaml is still
                     what the database actually returns. Costs nothing, runs in
                     CI on every commit, and catches the failure mode where the
                     dataset silently rots against a rebuilt database.

  --live             Actually asks a model each question through the real agent
                     loop and scores the answer. Costs tokens, so it is opt-in
                     and runs on prompt changes, not on every push.

Exit code is non-zero when any case fails, so it gates a merge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pokedb

GOLDEN = Path(__file__).parent / "golden.yaml"


def load_cases() -> dict:
    try:
        import yaml
    except ImportError as e:
        print("pyyaml is required: pip install pyyaml", file=sys.stderr)
        raise SystemExit(2) from e
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


def norm(s: str) -> str:
    return re.sub(r"[\s,]+", "", s).lower()


# ---------------------------------------------------------------------------
# offline: is the ground truth still true?
# ---------------------------------------------------------------------------
def verify_ground_truth(cases: list[dict]) -> int:
    failures = 0
    for c in cases:
        sql = c.get("check_sql")
        if not sql:
            continue
        try:
            res = pokedb.run_sql(sql)
        except pokedb.SqlError as e:
            print(f"  FAIL {c['id']}: check_sql did not run — {e}")
            failures += 1
            continue
        flat = norm(json.dumps(res["rows"], default=str))
        missing = [v for v in c.get("expect", []) if norm(str(v)) not in flat]
        if missing:
            print(f"  FAIL {c['id']}: database no longer returns {missing}")
            print(f"       query gave: {res['rows'][:3]}")
            failures += 1
        else:
            print(f"  ok   {c['id']}: {res['rows'][:1]}")
    return failures


def check_prompt_version(data: dict) -> int:
    import agent

    if data.get("version") != agent.PROMPT_VERSION:
        print(
            f"  WARN dataset was reviewed against prompt {data.get('version')!r} "
            f"but the prompt is {agent.PROMPT_VERSION!r}.\n"
            "       Re-run with --live, then bump `version:` in golden.yaml."
        )
        return 1
    print(f"  ok   dataset matches prompt version {agent.PROMPT_VERSION}")
    return 0


# ---------------------------------------------------------------------------
# live: score a real model
# ---------------------------------------------------------------------------
async def ask(question: str, model: str) -> str:
    import httpx

    import agent

    agent.set_client(httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)))
    try:
        text = []
        async for chunk in agent.stream_chat(
            [{"role": "user", "content": question}], model=model
        ):
            if not chunk.startswith("data:"):
                continue
            ev = json.loads(chunk[5:].strip())
            if ev.get("type") == "delta":
                text.append(ev["text"])
        return "".join(text)
    finally:
        c = agent.client()
        agent.set_client(None)
        await c.aclose()


def score(case: dict, answer: str) -> list[str]:
    problems = []
    flat = norm(answer)
    for v in case.get("expect", []):
        if norm(str(v)) not in flat:
            problems.append(f"missing {v!r}")
    any_of = case.get("expect_any") or []
    if any_of and not any(norm(str(v)) in flat for v in any_of):
        problems.append(f"none of {any_of}")
    for v in case.get("forbid", []):
        if v.lower() in answer.lower():
            problems.append(f"contains forbidden {v!r}")
    return problems


async def run_live(cases: list[dict], model: str) -> int:
    failures = 0
    for c in cases:
        try:
            answer = await ask(c["question"], model)
        except Exception as e:
            print(f"  FAIL {c['id']}: {type(e).__name__}: {e}")
            failures += 1
            continue
        problems = score(c, answer)
        if problems:
            failures += 1
            print(f"  FAIL {c['id']}: {'; '.join(problems)}")
            print(f"       answer: {answer[:220].replace(chr(10), ' ')}…")
        else:
            print(f"  ok   {c['id']}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="ask a real model (spends tokens)")
    ap.add_argument("--model", default="deepseek/deepseek-v3.2")
    args = ap.parse_args()

    data = load_cases()
    cases = data["cases"]
    print(f"golden dataset: {len(cases)} cases\n")

    print("ground truth (offline):")
    failures = verify_ground_truth(cases)
    print()
    warnings = check_prompt_version(data)

    if args.live:
        print(f"\nlive scoring against {args.model}:")
        failures += asyncio.run(run_live(cases, args.model))

    print()
    if failures:
        print(f"FAILED — {failures} case(s)")
        return 1
    print("passed" + (f" ({warnings} warning)" if warnings else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
