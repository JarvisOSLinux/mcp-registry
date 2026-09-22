#!/usr/bin/env bash
# Setup for the MPRIS media-control MCP server. CWD = install dir.
#
# Nothing is installed: the server is one file importing only the Python
# standard library. This script is the defense-in-depth environment check the
# registry asks for, and here it has real work to do -- unlike a server that
# only needs an interpreter, this one talks to the session bus through busctl,
# and both can be absent on a machine where Python is fine.
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

# busctl is how this server reaches D-Bus. It ships with systemd, so it is
# present on essentially every desktop Linux -- but "essentially every" is not
# "every", and without it no tool in this server can do anything at all, so
# this is a hard failure rather than a warning.
command -v busctl >/dev/null 2>&1 || {
  echo "busctl not found. It ships with systemd's tools; this server cannot reach D-Bus without it." >&2
  echo "  Arch: pacman -S systemd   Debian/Ubuntu: apt install systemd   Fedora: dnf install systemd" >&2
  exit 1
}

chmod +x server.py

# Import rather than merely parse: this is the exact interpreter and the exact
# module dmcp will spawn, so a missing stdlib module surfaces here, not on the
# user's first tool call.
python3 -c 'import server; assert server.TOOLS' >/dev/null

# A session bus at setup time is a good sign but not a requirement: setup can
# legitimately run from a console or a provisioning script, while the server
# itself will later be spawned inside the user's graphical session. Warn and
# continue rather than refusing an install that will be fine in practice.
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ ! -S "/run/user/$(id -u)/bus" ]; then
  echo "WARNING: no D-Bus session bus visible from this shell." >&2
  echo "         That is expected when installing outside a graphical session." >&2
  echo "         The server needs one at run time to see any media player." >&2
fi

echo "OK: MPRIS media-control MCP server ready (Python standard library only, no dependencies installed)"
echo "    Controls whatever is already playing locally. No account, no API key, no network access."
