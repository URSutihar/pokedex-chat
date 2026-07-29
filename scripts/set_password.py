#!/usr/bin/env python3
"""Change the app password. Takes effect on the next request — no restart.

    python scripts/set_password.py                 # prompt, hash, write
    python scripts/set_password.py --plain         # store as plain text instead
    python scripts/set_password.py --random        # generate a strong one, print it once
    python scripts/set_password.py --clear         # lock the server key entirely

The value is written to `.env` as `APP_PASSWORD=`; `config.Secrets` re-reads that
file whenever its mtime changes, so every browser is re-checked against the new
password on its next message.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ENV_PATH, hash_password

WORDS_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def write_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out, seen = [], False
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plain", action="store_true", help="store the password in clear text")
    ap.add_argument("--random", action="store_true", help="generate a 20-char password")
    ap.add_argument("--clear", action="store_true", help="remove the password (locks the key)")
    args = ap.parse_args()

    if args.clear:
        write_env_value(ENV_PATH, "APP_PASSWORD", "")
        print(f"cleared. {ENV_PATH} now locks the server key; visitors must bring their own.")
        return 0

    if args.random:
        pw = "".join(secrets.choice(WORDS_ALPHABET) for _ in range(20))
        print(f"\n  new password:  {pw}\n\n  Copy it now — it is not stored in readable form.\n")
    else:
        pw = getpass.getpass("new password: ")
        if len(pw) < 12:
            print("Too short. Use 12+ characters, or --random.", file=sys.stderr)
            return 1
        if pw != getpass.getpass("confirm     : "):
            print("They do not match.", file=sys.stderr)
            return 1

    write_env_value(ENV_PATH, "APP_PASSWORD", pw if args.plain else hash_password(pw))
    print(f"written to {ENV_PATH} ({'plain text' if args.plain else 'scrypt hash'}).")
    print("Live on the next request — no restart needed. Existing browsers will be")
    print("prompted again the moment they send their next message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
