#!/usr/bin/env bash
# Setup for the Notion wrapper. CWD = install dir (the cloned servers/notion).
#
# Nothing to install: the wrapper is standard-library Python and the server it
# fronts is hosted by Notion. This is the environment check the registry asks
# for -- it proves the interpreter this host will spawn can load the server.
set -euo pipefail

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found. Install Python 3.10+ (pacman -S python, apt install python3, dnf install python3)." >&2
  exit 1
}

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python >= 3.10 is required." >&2
  exit 1
}

chmod +x server.py
python3 -c 'import server; assert server.HANDLERS' >/dev/null

echo "OK: Notion wrapper ready"
