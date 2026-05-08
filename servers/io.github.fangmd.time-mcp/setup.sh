#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

need node

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 18 ]; then
  echo "Node.js >= 18 is required (found $(node -v))." >&2
  exit 1
fi

echo "Enabling corepack (for pnpm) and installing dependencies."
corepack enable
pnpm --version

pnpm install
pnpm build

echo "OK: built dist/index.js"

