#!/usr/bin/env bash
# Setup for the notifications MCP server. CWD = install dir (the cloned servers/notifications).
#
# Nothing to download: the server, its log module and its collector import only
# the Python standard library. Two things are set up:
#  1. the usual check that the interpreter dmcp will spawn can load the server;
#  2. the collector as a systemd *user* service (jarvis-notifications.service),
#     so notifications are logged while JARVIS isn't running and after a restart.
#     It needs no root: a user may watch their own session bus.
set -euo pipefail

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found. Install Python 3.10+ (e.g. pacman -S python, apt install python3, dnf install python3) before running setup." >&2
  exit 1
}

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  python_major_minor="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "Python >= 3.10 is required (found ${python_major_minor})." >&2
  exit 1
fi

# busctl (part of systemd) is how the collector watches notifications: it prints
# each D-Bus message as JSON, so no D-Bus library has to be installed.
command -v busctl >/dev/null 2>&1 || {
  echo "busctl not found. This server needs systemd's busctl to watch notifications." >&2
  exit 1
}

chmod +x server.py collector.py

# Import rather than merely parse: this is the exact interpreter and the exact
# modules dmcp will spawn, so a missing stdlib module surfaces here.
python3 -c 'import server, collector; assert server.TOOLS' >/dev/null

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/jarvis-notifications.service"
python_bin="$(command -v python3)"
mkdir -p "$unit_dir"
cat > "$unit" <<UNIT
[Unit]
Description=JARVIS notifications log (keeps this computer's notifications for "what did I miss?")
Documentation=https://github.com/JarvisOSLinux/mcp-registry/tree/main/servers/notifications
# Uninstalled (this folder removed): skip quietly instead of failing at every login.
ConditionPathExists=$PWD/collector.py

[Service]
ExecStart="$python_bin" "$PWD/collector.py"
Restart=on-failure
RestartSec=15
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT

echo "OK: notifications MCP server ready (Python standard library only, no dependencies installed)"
if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload >/dev/null 2>&1 \
   && systemctl --user enable jarvis-notifications.service >/dev/null 2>&1 \
   && systemctl --user restart jarvis-notifications.service >/dev/null 2>&1; then
  echo "    The collector runs as the user service jarvis-notifications.service."
else
  echo "    Couldn't start jarvis-notifications.service (no systemd user session here);"
  echo "    the server starts the collector itself the first time it is asked."
fi
echo "    Log: ${XDG_STATE_HOME:-$HOME/.local/state}/jarvis-notifications (owner-only, kept 7 days by default)."
echo "    To stop logging: systemctl --user disable --now jarvis-notifications.service"
