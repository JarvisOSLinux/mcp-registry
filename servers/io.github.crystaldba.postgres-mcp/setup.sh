#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

PINNED_VERSION="0.3.0"

# Prefer uv (fast); fall back to pip
if command -v uv >/dev/null 2>&1; then
  echo "Installing postgres-mcp==${PINNED_VERSION} via uv."
  uv pip install "postgres-mcp==${PINNED_VERSION}"
elif command -v pip3 >/dev/null 2>&1; then
  need python3
  echo "Installing postgres-mcp==${PINNED_VERSION} via pip3."
  pip3 install "postgres-mcp==${PINNED_VERSION}"
elif command -v pip >/dev/null 2>&1; then
  need python3
  echo "Installing postgres-mcp==${PINNED_VERSION} via pip."
  pip install "postgres-mcp==${PINNED_VERSION}"
else
  echo "No Python package manager found (uv, pip3, or pip required)." >&2
  exit 1
fi

echo "OK: postgres-mcp==${PINNED_VERSION} installed (run via: uvx postgres-mcp==${PINNED_VERSION} --access-mode=restricted)"
