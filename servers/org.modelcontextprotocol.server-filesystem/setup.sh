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

echo "Installing dependencies (npm ci) and building (npm run build)."
# npm >= 12 skips dependency install scripts not covered by allowScripts and
# still exits 0; re2's install script is what fetches/builds re2.node.
echo "allow-scripts=re2" > .npmrc
npm ci
npm run build
node -e "require('re2')"

echo "OK: built dist/index.js"

