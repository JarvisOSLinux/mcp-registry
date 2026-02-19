#!/usr/bin/env bash
# Setup script for Hello Config. CWD = install dir (project root with pyproject.toml).
set -e
pip install --quiet -e . 2>/dev/null || pip install --quiet .
python3 -c "import hello_config.server; print('OK')"
