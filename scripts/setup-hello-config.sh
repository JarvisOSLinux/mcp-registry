#!/usr/bin/env bash
# Setup script for Hello Config. CWD = install dir (project root with pyproject.toml).
# Uses a venv to avoid PEP 668 externally-managed-environment on Arch/modern distros.
set -e
command -v python3 >/dev/null 2>&1 || { echo "python3 not found. Install Python 3.10+ (e.g. pacman -S python, apt install python3, dnf install python3) before running setup." >&2; exit 1; }
python3 -m venv .venv
.venv/bin/pip install --quiet -e . 2>/dev/null || .venv/bin/pip install --quiet .
.venv/bin/python3 -c "import hello_config.server; print('OK')"
