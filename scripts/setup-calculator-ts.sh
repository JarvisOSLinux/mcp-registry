#!/usr/bin/env bash
# Setup script for Calculator (TypeScript). CWD = install dir (project root with package.json).
set -e
npm install
npm run build
