#!/usr/bin/env bash
# Setup for n8n-MCP (io.github.czlonkowski.n8n-mcp).
# Installs dependencies and builds the TypeScript server into dist/.
# The node-documentation database (data/nodes.db) is committed upstream, so no
# database rebuild is required here. Idempotent; safe to re-run via `dmcp setup`.
set -euo pipefail

if ! command -v node >/dev/null 2>&1; then
  echo "setup.sh: node is required but was not found on PATH (install Node.js 18+ / npm)." >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "setup.sh: npm is required but was not found on PATH." >&2
  exit 1
fi

# Reproducible install from the committed lockfile, then compile TS -> dist/.
if [ -f package-lock.json ]; then
  npm ci
else
  npm install
fi

npm run build

if [ ! -f dist/mcp/stdio-wrapper.js ]; then
  echo "setup.sh: build did not produce dist/mcp/stdio-wrapper.js — check the upstream build output." >&2
  exit 1
fi

if [ ! -f data/nodes.db ]; then
  echo "setup.sh: expected committed node database data/nodes.db is missing." >&2
  exit 1
fi

echo "n8n-MCP setup complete."
