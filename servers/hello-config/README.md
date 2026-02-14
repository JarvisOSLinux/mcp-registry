# Hello Config (MCP)

Minimal MCP server for testing:

- **configurableProperties**: required (e.g. API Key) and optional (timeout, endpoint with defaults)
- **scope**: `system` (install to `/usr/share/mcp/installed/`)

Provides a single `hello` tool. Run from repo root:

```bash
python3 src/hello_config/server.py
```

Or after `pip install -e .`: `mcp-hello-config`
