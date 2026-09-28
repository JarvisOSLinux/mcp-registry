#!/usr/bin/env bash
# Setup for the System Status MCP server. CWD = install dir.
#
# Nothing is installed: the server is one file importing only the Python
# standard library, and it reads the kernel's own interfaces. This script is the
# defense-in-depth environment check the registry asks for. It separates what
# the server cannot run without (Python, /proc) from what only narrows its
# answers (the system tools it asks), and fails only on the first kind.
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

# Every figure starts in /proc; without it there is nothing to report.
[ -r /proc/meminfo ] || {
  echo "/proc is not readable. This server reads Linux kernel interfaces and cannot run without them." >&2
  exit 1
}

chmod +x server.py

# Import rather than merely parse: this is the exact interpreter and the exact
# module dmcp will spawn, so a missing stdlib module surfaces here, not on the
# user's first tool call.
python3 -c 'import server; assert server.TOOLS' >/dev/null

# Optional tools. Each one missing removes only the part of an answer it
# provides, so these warn and continue rather than refusing the install.
missing=()
for tool in ip systemctl timedatectl loginctl nmcli; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "NOTE: not found: ${missing[*]}" >&2
  echo "      The server still runs; the answers those tools provide will read as unavailable:" >&2
  echo "      ip=network addresses/route, systemctl=failed services, timedatectl=timezone/clock sync," >&2
  echo "      loginctl=screen locked/idle, nmcli=Wi-Fi and connectivity." >&2
fi

echo "OK: system-status MCP server ready (Python standard library only, no dependencies installed)"
echo "    Read-only. Makes no network connections."
