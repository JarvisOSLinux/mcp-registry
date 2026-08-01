#!/usr/bin/env bash
# Setup for Sequential Thinking MCP (org.modelcontextprotocol.server-sequential-thinking).
#
# Upstream lives in the modelcontextprotocol/servers monorepo under
# src/sequentialthinking, and its tsconfig.json extends the repo-root
# ../../tsconfig.json. The whole repo is therefore cloned (source has no
# `path`), and only the one workspace package is built. Idempotent; safe to
# re-run via `dmcp setup`.
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "setup.sh: missing required command: $1" >&2
    exit 1
  }
}

need node
need npm

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 18 ]; then
  echo "setup.sh: Node.js >= 18 is required (found $(node -v 2>/dev/null || echo 'unknown'))." >&2
  exit 1
fi

echo "OK: Node.js $(node -v) and npm $(npm -v) detected."

# Reproducible install from the committed root lockfile. --ignore-scripts keeps
# the other six workspace packages from building via their prepare hooks; we
# build only the package we run. devDependencies (typescript, shx) are still
# installed, so the workspace build below has its toolchain.
echo "Installing workspace dependencies (npm ci --ignore-scripts)."
npm ci --ignore-scripts

echo "Building @modelcontextprotocol/server-sequential-thinking."
npm run build --workspace=@modelcontextprotocol/server-sequential-thinking

if [ ! -f src/sequentialthinking/dist/index.js ]; then
  echo "setup.sh: build did not produce src/sequentialthinking/dist/index.js -- check the upstream build output." >&2
  exit 1
fi

echo "OK: sequential-thinking ready (node src/sequentialthinking/dist/index.js)."
