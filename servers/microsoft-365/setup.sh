#!/usr/bin/env bash
# Setup for the Microsoft 365 wrapper. CWD = install dir (the cloned servers/microsoft-365).
#
# The wrapper is stdlib Python; the server it fronts is @softeria/ms-365-mcp-server,
# a Node package. This installs it into ./upstream at the version package.json pins,
# with every package checked against the committed package-lock.json (npm ci
# verifies each one's sha512 and refuses a lockfile that disagrees). No install
# scripts run. Optional packages are skipped: the archived keytar native module is
# not needed (cache_store.py keeps the token cache in the keyring), nor are the
# Azure Key Vault packages (server deployments only).
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

need python3
need node
need npm

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python >= 3.10 is required." >&2
  exit 1
}

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 20 ]; then
  echo "Node.js >= 20 is required (found $(node -v))." >&2
  exit 1
fi

rm -rf upstream
mkdir upstream
cp package.json package-lock.json upstream/
(
  cd upstream
  npm ci --ignore-scripts --omit=optional --no-audit --no-fund
)

test -f upstream/node_modules/@softeria/ms-365-mcp-server/dist/index.js
chmod +x server.py cache_store.py
python3 -c 'import server; assert server.HIDDEN_TOOLS' >/dev/null

echo "OK: wrapper ready, @softeria/ms-365-mcp-server $(node -p "require('./upstream/node_modules/@softeria/ms-365-mcp-server/package.json').version")"
