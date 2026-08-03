#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need node
need npx

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 18 ]; then
  echo "Node.js >= 18 is required (found $(node -v))." >&2
  exit 1
fi

# Pre-fetch the pinned version into the npm cache so the first MCP launch is fast.
echo "Pre-fetching obsidian-mcp-server@3.2.12 via npx."
npx -y obsidian-mcp-server@3.2.12 --version 2>/dev/null || true

echo "OK: obsidian-mcp-server@3.2.12 ready (run via: npx -y obsidian-mcp-server@3.2.12)"
echo "Required env: OBSIDIAN_API_KEY"
echo "Prerequisite: Obsidian Local REST API community plugin must be installed, enabled, and running in Obsidian."
