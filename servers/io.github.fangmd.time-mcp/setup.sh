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
if [ "${node_major:-0}" -lt 18 ]; then
  echo "Node.js >= 18 is required (found $(node -v))." >&2
  exit 1
fi

# corepack is NOT implied by node: distributions ship it separately (Arch has a
# standalone `corepack` package) and upstream Node dropped it from the default
# install in 25. Prefer `corepack pnpm` over `corepack enable` — enable writes
# shims into the Node prefix, which needs root on a system-wide install.
if command -v corepack >/dev/null 2>&1; then
  pnpm_cmd="corepack pnpm"
elif command -v pnpm >/dev/null 2>&1; then
  pnpm_cmd="pnpm"
else
  echo "Missing pnpm: install corepack (pacman -S corepack, apt install nodejs-corepack) or pnpm." >&2
  exit 1
fi

echo "Installing dependencies with '${pnpm_cmd}'."
${pnpm_cmd} --version

${pnpm_cmd} install
${pnpm_cmd} build

echo "OK: built dist/index.js"

