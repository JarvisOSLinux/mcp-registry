#!/usr/bin/env bash
# Setup script for Calculator (Python). CWD = install dir (project root with pyproject.toml).
set -e
pip install --quiet -e . 2>/dev/null || pip install --quiet .
python3 -c "import mcp.server.fastmcp; print('OK')"
