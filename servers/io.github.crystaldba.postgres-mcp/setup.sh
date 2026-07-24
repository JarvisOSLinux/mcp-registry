#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

# Prefer uv (fast); fall back to pip
if command -v uv >/dev/null 2>&1; then
  echo "Installing postgres-mcp via uv."
  uv pip install postgres-mcp
elif command -v pip3 >/dev/null 2>&1; then
  need python3
  echo "Installing postgres-mcp via pip3."
  pip3 install postgres-mcp
elif command -v pip >/dev/null 2>&1; then
  need python3
  echo "Installing postgres-mcp via pip."
  pip install postgres-mcp
else
  echo "No Python package manager found (uv, pip3, or pip required)." >&2
  exit 1
fi

echo "OK: postgres-mcp installed (run via: uvx postgres-mcp)"
