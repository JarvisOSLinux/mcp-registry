"""Slow MCP Server — introduces configurable delays to test REMIND signals in dispatch."""

import asyncio
import time

from mcp.server.fastmcp import FastMCP

SERVER_ID = "slow-mcp"
mcp = FastMCP(SERVER_ID)


@mcp.tool()
async def slow_echo(message: str, delay_seconds: int = 5) -> str:
    """Wait for a specified number of seconds, then return the message.

    Set dispatch remind_after below this delay to trigger a REMIND signal
    before the task completes. Use delay_seconds=0 for an instant response.

    Args:
        message: The message to echo back after the delay.
        delay_seconds: How long to wait before responding (default: 5).
    """
    started = time.strftime("%H:%M:%S")
    await asyncio.sleep(delay_seconds)
    finished = time.strftime("%H:%M:%S")
    return (
        f"[{SERVER_ID}] slow_echo complete\n"
        f"  message:  {message}\n"
        f"  delay:    {delay_seconds}s\n"
        f"  started:  {started}\n"
        f"  finished: {finished}"
    )


@mcp.tool()
async def ping() -> str:
    """Instant liveness check. Returns server identity and current timestamp.

    Dispatch alongside slow_echo to observe a fast EXIT and a slow EXIT
    in the same signal window.
    """
    return f"[{SERVER_ID}] pong — {time.strftime('%H:%M:%S')}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
