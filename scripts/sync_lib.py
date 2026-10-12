#!/usr/bin/env python3
"""Refresh the vendored copies of servers/_lib/.

dmcp installs only a server's own directory (`source.path`), so a wrapper cannot
import a sibling `servers/_lib/` at runtime. Each wrapper therefore carries a
byte-identical copy at `servers/<wrapper>/_lib/`; this script rewrites every such
copy from the canonical source, and `validate_registry.py` fails the PR gate if
one drifts.

    python scripts/sync_lib.py          # rewrite the copies
    python scripts/sync_lib.py --check  # exit 1 if any copy differs
"""

import argparse
import shutil
import sys
from pathlib import Path

SERVERS_DIR = Path(__file__).resolve().parent.parent / "servers"
CANONICAL = SERVERS_DIR / "_lib"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def tree(root: Path) -> dict:
    """Relative path -> bytes for every file under root, ignoring bytecode."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def vendored_copies(servers_dir: Path = SERVERS_DIR) -> list[Path]:
    return sorted(p for p in servers_dir.glob("*/_lib") if p.is_dir() and p.parent.name != "_lib")


def differing(servers_dir: Path = SERVERS_DIR) -> list[Path]:
    canonical = tree(servers_dir / "_lib")
    return [copy for copy in vendored_copies(servers_dir) if tree(copy) != canonical]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report drift, change nothing")
    args = parser.parse_args()

    stale = differing()
    if args.check:
        for copy in stale:
            print(f"{copy.relative_to(SERVERS_DIR.parent)}: differs from servers/_lib")
        return 1 if stale else 0

    for copy in stale:
        shutil.rmtree(copy)
        shutil.copytree(CANONICAL, copy, ignore=IGNORE)
        print(f"updated {copy.relative_to(SERVERS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
