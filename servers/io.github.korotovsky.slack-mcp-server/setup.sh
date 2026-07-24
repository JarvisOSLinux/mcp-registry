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

REPO_DIR="$(dirname "$0")/slack-mcp-server-src"

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "Cloning slack-mcp-server repository."
  git clone https://github.com/korotovsky/slack-mcp-server.git "$REPO_DIR"
else
  echo "Updating slack-mcp-server repository."
  git -C "$REPO_DIR" pull --ff-only
fi

cd "$REPO_DIR"
echo "Installing dependencies and building slack-mcp-server."
npm install
npm run build

echo "OK: slack-mcp-server built at $REPO_DIR/dist/index.js"
echo "Run via: node dist/index.js (from $REPO_DIR)"
echo "Required env: SLACK_BOT_TOKEN, SLACK_TEAM_ID"
