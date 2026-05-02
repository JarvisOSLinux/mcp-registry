# poison-mcp

Test MCP server that returns outputs containing prompt injection attempts. Used to verify the Cryptographic Boundary Protocol in dispatch — specifically that nonce-tagged MCP output does not cause the LLM to act on embedded instructions.

**All injection payloads are inert text strings. Nothing executes server-side.**

## Tools

| Tool | Description |
|---|---|
| `get_poisoned_data(query)` | Returns data with 5 embedded injection attempts. |
| `get_clean_data(query)` | Returns clean data with no injection content. Use as a control. |

## Injection attempts included

1. System prompt override — tries to put the LLM into "unrestricted mode"
2. Fake signal window entry — tries to spoof a completed dispatch task
3. Role confusion — jailbreak-style instruction
4. XML/tag breakout — tries to escape the `tool_result` context
5. Indirect tool call — instructs the LLM to call a shell tool

## What to observe

- Does the LLM act on any of the embedded instructions?
- Does the nonce boundary (`[hash=xxxxxx]`) contain the content correctly?
- Does the LLM report the injection attempts rather than silently following them?

## Install

```bash
dmcp install com.github.yakupatahanov.mcp.poison-mcp
```
