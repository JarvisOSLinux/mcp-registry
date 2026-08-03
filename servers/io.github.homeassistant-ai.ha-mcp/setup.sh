#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need node
need npm
need git

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 18 ]; then
  echo "Node.js >= 18 is required (found $(node -v))." >&2
  exit 1
fi

# Pinned to v7.0.0 (SHA 1f2bbbc74e81933a39e9c63998d1408f0c198309)
# Fixes CVE-2026-32111 (SSRF in OAuth DCR mode). Do NOT update without re-auditing.
PINNED_SHA="1f2bbbc74e81933a39e9c63998d1408f0c198309"
REPO_DIR="$(dirname "$0")/ha-mcp-src"

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "Cloning ha-mcp repository."
  git clone https://github.com/homeassistant-ai/ha-mcp.git "$REPO_DIR"
fi

echo "Pinning ha-mcp to SHA ${PINNED_SHA} (v7.0.0)."
git -C "$REPO_DIR" fetch origin
git -C "$REPO_DIR" checkout "${PINNED_SHA}"

cd "$REPO_DIR"
echo "Installing dependencies and building ha-mcp server."
npm install
npm run build

echo "OK: ha-mcp v7.0.0 built at $REPO_DIR/dist/index.js"
echo "Run via: node ha-mcp-src/dist/index.js (from manifest directory)"
echo "Required env: HA_BASE_URL, HA_TOKEN"
