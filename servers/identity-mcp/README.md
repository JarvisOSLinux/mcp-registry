# identity-mcp

Every response from this server clearly identifies itself. Use it to confirm that server discovery, tool routing, and the LLM's server awareness are working correctly — you can always tell which server responded.

## Tools

| Tool | Description |
|---|---|
| `whoami()` | Returns server ID, version, and timestamp. |
| `echo(message)` | Echoes the message back with server identity prefix. |
| `ping()` | Instant liveness check with server identity. |

## Test scenarios

- Dispatch `ping` alongside tasks from other servers — confirm each EXIT identifies its source.
- Use `echo` to verify parameter passing is working correctly end-to-end.
- Use `whoami` at the start of a session to confirm which server the LLM discovered.

## Install

```bash
dmcp install com.github.yakupatahanov.mcp.identity-mcp
```
