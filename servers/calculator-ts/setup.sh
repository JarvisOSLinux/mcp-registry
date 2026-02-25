#!/usr/bin/env bash
# Setup script for Calculator (TypeScript). CWD = install dir (project root with package.json).
# Installs Node.js/npm if missing (via system package manager).
set -e
if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found. Installing Node.js and npm..."
  if command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm nodejs npm
  elif command -v apt >/dev/null 2>&1; then
    sudo apt update && sudo apt install -y nodejs npm
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y nodejs npm
  else
    echo "Could not detect package manager. Install Node.js and npm manually." >&2
    exit 1
  fi
fi
npm install
npm run build
