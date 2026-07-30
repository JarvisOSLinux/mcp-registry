#!/usr/bin/env bash
# Setup for the CalDAV MCP server. CWD = install dir (the cloned servers/caldav).
#
# There is nothing to install: the server is one file and imports only the
# Python standard library. This script is the defense-in-depth environment
# check the registry asks for — it proves the interpreter this host will spawn
# can actually load the server before dmcp ever tries.
set -euo pipefail

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found. Install Python 3.10+ (e.g. pacman -S python, apt install python3, dnf install python3) before running setup." >&2
  exit 1
}

# Compare numerically in Python itself: an allow-list of known-good versions
# rejects every future release. 3.10 is the floor — the server annotates with
# PEP 604 unions, which earlier interpreters evaluate and reject at import.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  python_major_minor="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "Python >= 3.10 is required (found ${python_major_minor})." >&2
  exit 1
fi

# zoneinfo is stdlib, but it reads the system tz database. Without one, an event
# another client wrote with a TZID is reported as a floating time instead of one
# carrying its real offset. Events this server writes are normalized to UTC and
# are unaffected, so this degrades rather than fails — say so and continue.
python3 -c 'import zoneinfo; zoneinfo.ZoneInfo("America/Los_Angeles")' 2>/dev/null || {
  echo "WARNING: no system timezone database found (zoneinfo cannot load a named zone)." >&2
  echo "         Events written by this server (UTC) are unaffected; events written" >&2
  echo "         elsewhere with a TZID will be reported without their offset." >&2
  echo "         Install tzdata to fix (e.g. pacman -S tzdata, apt install tzdata)." >&2
}

chmod +x server.py

# Import rather than merely parse: this is the exact interpreter and the exact
# module dmcp will spawn, so a missing stdlib module surfaces here, not on the
# user's first tool call.
python3 -c 'import server; assert server.TOOLS' >/dev/null

echo "OK: caldav MCP server ready (Python standard library only, no dependencies installed)"
