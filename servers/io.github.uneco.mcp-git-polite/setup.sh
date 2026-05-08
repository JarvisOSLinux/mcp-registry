#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

need python3
need uv
need git

python_major_minor="$(python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')"
case "${python_major_minor}" in
  3.10|3.11|3.12|3.13) ;;
  *)
    echo "Python >= 3.10 is required (found ${python_major_minor})." >&2
    exit 1
    ;;
esac

uv sync
echo "OK: uv environment synced"

