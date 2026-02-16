# Hello World (WebSocket)

Minimal MCP server over WebSocket. For testing remote/metadata-only entries in Discover.

Run locally (use a different port than SSE):

```bash
cd servers/hello-ws && uvicorn app:app --host 127.0.0.1 --port 8001
```

Connect at: `ws://127.0.0.1:8001/ws`
