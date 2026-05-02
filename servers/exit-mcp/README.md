# exit-mcp

Test MCP server that produces controlled success and failure exits. Use it to verify the dispatch signal paths for `200` and `500` exits, inline output, and mixed-state batches.

## Tools

| Tool | Description |
|---|---|
| `succeed(message)` | Return a successful result immediately (200 EXIT). |
| `fail(message)` | Raise an exception to produce a 500 EXIT. |
| `succeed_after(message, delay_seconds)` | Wait, then return success. Combine with `fail()` in a batch to test mixed exits. |

## Test scenarios

- Dispatch `succeed` and `fail` in parallel — observe one `200` and one `500` in the signal window.
- Dispatch `succeed_after` with a short delay alongside `fail` — fast failure, slow success.
- Dispatch multiple `fail` calls — verify all errors are nonce-tagged and surface correctly.

## Install

```bash
dmcp install com.github.yakupatahanov.mcp.exit-mcp
```
