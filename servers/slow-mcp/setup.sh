#!/usr/bin/env bash
set -e
command -v python3 >/dev/null 2>&1 || { echo "python3 not found. Install Python 3.10+." >&2; exit 1; }
python3 -m venv .venv
.venv/bin/pip install --quiet -e . 2>/dev/null || .venv/bin/pip install --quiet .
.venv/bin/python3 -c "import mcp.server.fastmcp; print('OK')"
