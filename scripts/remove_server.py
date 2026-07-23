#!/usr/bin/env python3
"""remove_server.py — hard-excise a server from the registry.

Atomically removes, for a given server id:
  - the servers/<dir>/ directory (manifest.json, setup.sh, sources, ...)
  - the registry.json entry, including its inline embeddings + integrity hashes
  - and bumps the top-level `updated` timestamp

Hard deletion is destructive: it breaks anyone who has the server installed or
pinned by commit, and it drops the provenance/audit trail. The registry's trust
model (docs/TRUST-MODEL.md) prefers *soft revocation* first — set
`trustStatus: removed` so dmcp refuses new installs while existing installs keep
working — and only hard-excise after a grace period. To enforce that ordering
this script refuses to excise a still-live entry (trustStatus other than
`removed`/`deprecated`) unless `--force` is passed.

Usage:
  python3 scripts/remove_server.py <server_id>
  python3 scripts/remove_server.py <server_id> --force   # excise even if live
  python3 scripts/remove_server.py <server_id> --check    # dry-run; exit 1 if it would change
"""
import argparse
import datetime
import json
import pathlib
import shutil
import sys

from sync_registry import dir_from_url

REGISTRY = pathlib.Path("registry.json")
SERVERS_DIR = pathlib.Path("servers")

# trustStatus values that make an entry safe to hard-excise without --force.
EXCISABLE_WITHOUT_FORCE = {"removed", "deprecated"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("server_id", help="The registry map key / entry id to excise")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Excise even a live entry (trustStatus not removed/deprecated)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry run: report what would be removed and exit 1 (no writes)",
    )
    args = parser.parse_args()

    registry = json.loads(REGISTRY.read_text())
    servers = registry.get("servers", {})
    sid = args.server_id

    if sid not in servers:
        print(f"ERROR: '{sid}' is not a registry entry. Nothing to excise.")
        return 1

    entry = servers[sid]
    trust = entry.get("trustStatus")
    dir_name = dir_from_url(entry.get("manifest", ""))
    local_dir = SERVERS_DIR / dir_name if dir_name else None

    if trust not in EXCISABLE_WITHOUT_FORCE and not args.force:
        print(
            f"REFUSED: '{sid}' has trustStatus '{trust}', which is still live.\n"
            f"  Soft-revoke it first (set trustStatus: removed) so existing installs\n"
            f"  keep working, then excise — or pass --force to hard-delete now."
        )
        return 1

    print(f"Excise plan for '{sid}' (trustStatus: {trust}):")
    print(f"  - drop registry.json entry (incl. inline embeddings + integrity hashes)")
    if local_dir and local_dir.exists():
        print(f"  - remove directory {local_dir}/")
    elif dir_name:
        print(f"  - (no local directory {local_dir}/ to remove)")
    else:
        print(f"  - (manifest URL has no local dir; only the entry is removed)")

    if args.check:
        print("\n--check: dry run only, no files written.")
        return 1

    del servers[sid]
    if local_dir and local_dir.exists():
        shutil.rmtree(local_dir)

    registry["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"\nExcised '{sid}'. registry.json updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
