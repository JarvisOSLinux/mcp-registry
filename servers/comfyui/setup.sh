#!/usr/bin/env bash
# Setup for the JARVIS ComfyUI MCP server.
# CWD = install directory (the cloned servers/comfyui directory).
#
# This server uses Python stdlib only (urllib, json, base64).
# ComfyUI itself must be installed and running separately — this script
# verifies the interpreter and optionally checks that ComfyUI is reachable.
#
# Re-runnable and safe to run without root.
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Verify Python 3.9+
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}python3 not found. Install Python 3.9+.${NC}" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    found="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo -e "${RED}Python >= 3.9 required (found ${found}).${NC}" >&2
    exit 1
fi

# All deps are stdlib — just verify the module loads
python3 -B -c "
import importlib.util
spec = importlib.util.spec_from_file_location('server', 'server.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
" || {
    echo -e "${RED}server.py failed to load (see traceback above).${NC}" >&2
    exit 1
}

chmod +x server.py
echo -e "${GREEN}server.py loads cleanly (stdlib only — no packages to install).${NC}"

# Optional connectivity check
COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
echo "Checking ComfyUI at ${COMFYUI_URL} ..."
if python3 -c "
import urllib.request, sys
try:
    urllib.request.urlopen('${COMFYUI_URL}/system_stats', timeout=5)
    sys.exit(0)
except Exception as e:
    print(f'  Not reachable: {e}')
    sys.exit(1)
" 2>/dev/null; then
    echo -e "${GREEN}ComfyUI is reachable at ${COMFYUI_URL}.${NC}"
else
    echo -e "${YELLOW}ComfyUI is not reachable at ${COMFYUI_URL} right now.${NC}"
    echo "That is OK — ComfyUI does not need to be running during setup."
    echo "Start ComfyUI before using this server:"
    echo "  cd /path/to/ComfyUI && python main.py --listen 127.0.0.1 --port 8188"
    echo "Override the URL via the COMFYUI_URL environment variable."
fi

echo ""
echo -e "${GREEN}Setup complete.${NC}"
echo ""
echo "Install ComfyUI if you haven't already:"
echo "  git clone https://github.com/comfyanonymous/ComfyUI"
echo "  cd ComfyUI && pip install -r requirements.txt"
echo "  # Download a checkpoint to ComfyUI/models/checkpoints/"
echo "  python main.py --listen 127.0.0.1 --port 8188"
echo ""
echo "Then start using generate_image with the model filename from list_models."
