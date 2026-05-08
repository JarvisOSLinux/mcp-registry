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
if [ "${node_major:-0}" -lt 22 ]; then
  echo "Node.js >= 22 is required (found $(node -v))." >&2
  exit 1
fi

echo "Enabling corepack (for yarn) and installing dependencies."
corepack enable
yarn --version

yarn install --frozen-lockfile
yarn build

echo "OK: built dist/index.js"

