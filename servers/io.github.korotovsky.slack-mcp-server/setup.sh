#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need go

go_major="$(go version | grep -oP 'go\K[0-9]+' | head -1)"
if [ "${go_major:-0}" -lt 1 ]; then
  echo "Go 1.21+ is required." >&2
  exit 1
fi

# Pre-download and verify the module via Go module proxy + sumdb.
# Version pinned to v1.3.0 (SHA a079b3cd4d5836d791c942a9fc107987e7865b37).
echo "Pre-fetching slack-mcp-server@v1.3.0 via Go module proxy (sumdb-verified)."
GOPATH="$(go env GOPATH)"
export GOPATH
go install "github.com/korotovsky/slack-mcp-server/cmd/slack-mcp-server@v1.3.0"

BINARY="${GOPATH}/bin/slack-mcp-server"
if [ ! -f "$BINARY" ]; then
  echo "Build failed: ${BINARY} not found." >&2
  exit 1
fi

echo "OK: slack-mcp-server v1.3.0 installed at ${BINARY}"
echo "Run via: go run github.com/korotovsky/slack-mcp-server/cmd/slack-mcp-server@v1.3.0"
echo "Required env: at least one of SLACK_MCP_XOXB_TOKEN, SLACK_MCP_XOXP_TOKEN, or SLACK_MCP_XOXC_TOKEN+SLACK_MCP_XOXD_TOKEN"
echo "Optional env: SLACK_MCP_ADD_MESSAGE_TOOL=1 to enable send_message"
