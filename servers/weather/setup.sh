#!/usr/bin/env bash
# Setup for the Weather MCP server. CWD = install dir (the cloned servers/weather).
#
# There is nothing to install: the server is one file and imports only the
# Python standard library, and Open-Meteo needs no account or API key. This
# script is the defense-in-depth environment check the registry asks for -- it
# proves the interpreter this host will spawn can actually load the server
# before dmcp ever tries.
set -euo pipefail

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found. Install Python 3.10+ (e.g. pacman -S python, apt install python3, dnf install python3) before running setup." >&2
  exit 1
}

# Compare numerically in Python itself: an allow-list of known-good versions
# rejects every future release. 3.10 is the floor -- the server annotates with
# PEP 604 unions, which earlier interpreters evaluate and reject at import.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  python_major_minor="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "Python >= 3.10 is required (found ${python_major_minor})." >&2
  exit 1
fi

chmod +x server.py

# Import rather than merely parse: this is the exact interpreter and the exact
# module dmcp will spawn, so a missing stdlib module surfaces here, not on the
# user's first tool call.
python3 -c 'import server; assert server.TOOLS' >/dev/null

# Not a reachability test -- a laptop set up offline must still install. This
# only says so plainly, because every tool needs outbound HTTPS at call time.
echo "OK: weather MCP server ready (Python standard library only, no dependencies installed)"
echo "    Open-Meteo needs no API key. Tools require outbound HTTPS to api.open-meteo.com."
