#!/usr/bin/env python3
"""Softeria's auth-cache command, backed by the shared token store.

Softeria (@softeria/ms-365-mcp-server) keeps its MSAL token cache in keytar or,
without it, in an encrypted file whose key sits beside it. Pointed at this script
through MS365_MCP_AUTH_CACHE_COMMAND, it stores the cache through `_lib.tokenstore`
instead: the OS keyring via secret-tool, else an owner-only file. Microsoft
refresh tokens live for months, so they belong in the keyring.

Protocol v1, as Softeria documents it:

    cache_store.py load <key>    -> {"found": true, "value": "..."} or {"found": false}
    cache_store.py save <key>    <- {"value": "..."} on stdin; exit 0 once committed
    cache_store.py delete <key>  -> exit 0 whether or not it existed

<key> is `token-cache` or `selected-account`. Any other exit status is a storage
error, and Softeria fails closed on it. Stdout carries only the JSON answer.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import tokenstore  # noqa: E402

KEYS = ("token-cache", "selected-account")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("load", "save", "delete") or argv[1] not in KEYS:
        print("usage: cache_store.py load|save|delete token-cache|selected-account", file=sys.stderr)
        return 64
    action, key = argv
    store = tokenstore.TokenStore(f"microsoft-365.{key}")
    try:
        if action == "load":
            record = store.load()
            value = record.get("value") if isinstance(record, dict) else None
            print(json.dumps({"found": True, "value": value} if isinstance(value, str) else {"found": False}))
        elif action == "save":
            payload = json.loads(sys.stdin.read())
            value = payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(value, str):
                print("save: stdin must be {\"value\": \"...\"}", file=sys.stderr)
                return 65
            store.save({"value": value})
        else:
            store.delete()
    except (tokenstore.StoreError, OSError, ValueError) as err:
        print(f"{action} {key}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
