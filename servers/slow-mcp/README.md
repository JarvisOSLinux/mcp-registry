# slow-mcp

Test MCP server that introduces configurable response delays. Use it to trigger `REMIND` signals in the dispatch workflow and test timeout handling.

## Tools

| Tool | Description |
|---|---|
| `slow_echo(message, delay_seconds)` | Wait `delay_seconds`, then return the message. Default delay: 5s. |
| `ping()` | Instant response. Baseline liveness check. |

## Test scenarios

- Set `remind_after: 3` on a dispatch task using `slow_echo` with `delay_seconds: 10` — REMIND fires before the task completes.
- Dispatch `slow_echo` and `ping` in parallel — `ping` exits immediately, `slow_echo` holds up the batch.
- Dispatch multiple `slow_echo` calls with different delays to test mixed completion timing.

## Install

```bash
dmcp install com.github.yakupatahanov.mcp.slow-mcp
```
