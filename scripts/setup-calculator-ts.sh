#!/usr/bin/env bash
# Setup script for Calculator (TypeScript). CWD = install dir (project root with package.json).
# Requires Node.js and npm (e.g. pacman -S nodejs npm).
set -e
command -v npm >/dev/null 2>&1 || { echo "npm not found. Install Node.js and npm (e.g. pacman -S nodejs npm, apt install nodejs npm, dnf install nodejs npm) before running setup." >&2; exit 1; }
npm install
npm run build
