#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

need node

node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
if [ "${node_major:-0}" -lt 22 ]; then
  echo "Node.js >= 22 is required (found $(node -v))." >&2
  exit 1
fi

# corepack is NOT implied by node: distributions ship it separately (Arch has a
# standalone `corepack` package) and upstream Node dropped it from the default
# install in 25. Prefer `corepack yarn` over `corepack enable` — enable writes
# shims into the Node prefix, which needs root on a system-wide install.
if command -v corepack >/dev/null 2>&1; then
  yarn_cmd="corepack yarn"
elif command -v yarn >/dev/null 2>&1; then
  yarn_cmd="yarn"
else
  echo "Missing yarn: install corepack (pacman -S corepack, apt install nodejs-corepack) or yarn." >&2
  exit 1
fi

echo "Installing dependencies with '${yarn_cmd}'."
${yarn_cmd} --version

${yarn_cmd} install --frozen-lockfile
${yarn_cmd} build

echo "OK: built dist/index.js"

