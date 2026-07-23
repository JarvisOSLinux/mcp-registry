#!/bin/sh
set -eu

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need node
need npm

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 18 ]; then
  echo "Node.js >= 18 is required (found $(node -v 2>/dev/null || echo 'unknown'))." >&2
  exit 1
fi

echo "Installing dependencies (npm ci)."
npm ci

# No-op (`echo OK`) at v0.0.78 — the repo ships a runnable cli.js — but kept
# so a future tag that reintroduces a real build step still works.
npm run build

echo "Downloading Chromium (npx playwright install chromium)."
npx playwright install chromium

echo "Note: Chromium may require system libraries that this script does not"
echo "install. If browser launch fails, install them with your distro's"
echo "package manager (the equivalent of 'playwright install-deps chromium')."

echo "OK: playwright-mcp ready (node cli.js --headless)."
