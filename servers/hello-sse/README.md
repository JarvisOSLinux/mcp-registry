# Hello World (SSE)

Minimal MCP server over Server-Sent Events. For testing remote/metadata-only entries in Discover.

Run locally:

```bash
cd servers/hello-sse && uvicorn app:app --host 127.0.0.1 --port 8000
```

Connect at: `http://127.0.0.1:8000/sse`
