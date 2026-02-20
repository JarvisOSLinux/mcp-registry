#!/usr/bin/env bash
# Setup script for Hello Config. CWD = install dir (project root with pyproject.toml).
# Uses a venv to avoid PEP 668 externally-managed-environment on Arch/modern distros.
set -e
python3 -m venv .venv
.venv/bin/pip install --quiet -e . 2>/dev/null || .venv/bin/pip install --quiet .
.venv/bin/python3 -c "import hello_config.server; print('OK')"
