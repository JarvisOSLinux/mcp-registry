#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

need python3

py_ok="$(python3 - <<'PY'
import sys
print(1 if sys.version_info[:2] >= (3, 10) else 0)
PY
)"
if [ "${py_ok:-0}" != "1" ]; then
  echo "Python >= 3.10 is required (found $(python3 -V 2>&1))." >&2
  exit 1
fi

echo "Creating virtualenv (.venv) and installing blender-mcp and its dependencies."
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .

echo "OK: installed blender-mcp into .venv (entry point: .venv/bin/blender-mcp)"
echo
echo "NOTE: This server only controls Blender through a companion addon. Before use you must:"
echo "  1. Install Blender >= 3.0."
echo "  2. In Blender: Edit > Preferences > Add-ons > Install, select addon.py from this repo, and enable 'Interface: Blender MCP'."
echo "  3. In the 3D viewport sidebar (press N) open the 'BlenderMCP' tab and click 'Connect to MCP server' so the addon listens on localhost:9876."
echo
echo "TELEMETRY: upstream blender-mcp ships opt-out telemetry to a hardcoded Supabase project. The registry manifest disables it by default (config DISABLE_TELEMETRY=1, injected as an env var at launch). Clear that config value to opt back in."
