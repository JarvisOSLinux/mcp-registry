#!/usr/bin/env bash
# Setup script for remote (SSE/WebSocket) servers. CWD = install dir (only manifest.json).
# Reads user config from manifest.json and writes client-env.sh so the local client
# can use it when connecting to the remote server.
set -e
MANIFEST="${1:-./manifest.json}"
if [[ ! -f "$MANIFEST" ]]; then
  echo "No manifest at $MANIFEST" >&2
  exit 1
fi
# Use Python to read JSON (portable; no jq required)
export MANIFEST
python3 << 'PY'
import json
import os
with open(os.environ.get("MANIFEST", "manifest.json")) as f:
    m = json.load(f)
config = m.get("config", {})
transports = m.get("transports", [])
url = None
ws_url = None
if transports:
    t = transports[0]
    url = t.get("url")
    ws_url = t.get("wsUrl")
lines = []
for k, v in (config or {}).items():
    if v is not None and v != "":
        # Escape for shell
        v = str(v).replace("'", "'\"'\"'")
        lines.append(f"export {k}='{v}'")
if url:
    lines.append(f"export MCP_SSE_URL='{url}'")
if ws_url:
    lines.append(f"export MCP_WS_URL='{ws_url}'")
with open("client-env.sh", "w") as out:
    out.write("# Generated from manifest.json for local client use\n")
    out.write("\n".join(lines))
    out.write("\n")
print("Wrote client-env.sh with config and transport URLs")
PY
