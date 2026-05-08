#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

need node
need npm

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 24 ]; then
  echo "Node.js >= 24 is required (found $(node -v))." >&2
  echo "Install a newer Node.js, then re-run setup." >&2
  exit 1
fi

echo "OK: Node.js $(node -v) and npm $(npm -v) detected."
echo "This server runs via npx at runtime; no build step is performed here."

