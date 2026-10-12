#!/usr/bin/env bash
# Setup for the GitHub wrapper. CWD = install dir (the cloned servers/github).
#
# The wrapper is stdlib Python; the server it fronts is MissionSquad/mcp-github,
# a Node project. This script fetches that server at one pinned commit into
# ./upstream and builds it. A moved ref cannot substitute different code: the
# checked-out HEAD must be exactly the pinned SHA.
set -euo pipefail

UPSTREAM_URL="https://github.com/MissionSquad/mcp-github.git"
UPSTREAM_REV="e312daffca07981c02aa8ca13a9dee82ad51697c"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

need python3
need git
need node

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python >= 3.10 is required." >&2
  exit 1
}

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 22 ]; then
  echo "Node.js >= 22 is required (found $(node -v))." >&2
  exit 1
fi

# corepack is not implied by node (see the mcp-github entry); prefer
# `corepack yarn` over `corepack enable`, which needs root on a system Node.
if command -v corepack >/dev/null 2>&1; then
  yarn_cmd="corepack yarn"
elif command -v yarn >/dev/null 2>&1; then
  yarn_cmd="yarn"
elif command -v npm >/dev/null 2>&1; then
  # No corepack and no yarn: install yarn 1 (what upstream's lockfile is) into
  # this server's own directory with npm. No root, nothing global.
  echo "yarn not found; installing yarn 1 locally with npm."
  npm install --prefix "$PWD/.tools" --no-audit --no-fund yarn@1.22.22
  yarn_cmd="$PWD/.tools/node_modules/.bin/yarn"
else
  echo "Missing yarn and npm: install Node.js with npm, corepack or yarn." >&2
  exit 1
fi

rm -rf upstream
git clone --filter=blob:none "$UPSTREAM_URL" upstream
git -C upstream checkout --detach "$UPSTREAM_REV"
head="$(git -C upstream rev-parse HEAD)"
if [ "$head" != "$UPSTREAM_REV" ]; then
  echo "Upstream checkout is $head, expected $UPSTREAM_REV." >&2
  exit 1
fi

(
  cd upstream
  ${yarn_cmd} install --frozen-lockfile
  ${yarn_cmd} build
)
rm -rf upstream/.git

test -f upstream/dist/index.js
chmod +x server.py
python3 -c 'import server; assert server.HANDLERS' >/dev/null

echo "OK: wrapper ready, upstream built at ${UPSTREAM_REV}"
