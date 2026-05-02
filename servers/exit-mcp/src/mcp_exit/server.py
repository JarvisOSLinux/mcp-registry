"""Exit MCP Server — controlled success and failure exits for testing dispatch signal paths."""

import asyncio
import time

from mcp.server.fastmcp import FastMCP

SERVER_ID = "exit-mcp"
mcp = FastMCP(SERVER_ID)


@mcp.tool()
async def succeed(message: str) -> str:
    """Return a successful result immediately.

    Produces a 200 EXIT signal in dispatch. If inline_output is enabled,
    the message appears directly in the signal window.

    Args:
        message: Content to include in the success response.
    """
    return (
        f"[{SERVER_ID}] SUCCESS\n"
        f"  time:    {time.strftime('%H:%M:%S')}\n"
        f"  message: {message}"
    )


@mcp.tool()
async def fail(message: str) -> str:
    """Raise an exception to produce a 500 EXIT signal in dispatch.

    The error content will be nonce-tagged in the signal window:
    [hash=xxxxxx] 500 <xxxxxx>INTENTIONAL FAILURE: {message}</xxxxxx>

    Args:
        message: Description of the failure shown in the error signal.
    """
    raise RuntimeError(f"[{SERVER_ID}] INTENTIONAL FAILURE: {message}")


@mcp.tool()
async def succeed_after(message: str, delay_seconds: int = 3) -> str:
    """Wait, then return a successful result.

    Combine with fail() in a parallel dispatch batch to observe
    mixed 200/500 exits in the same signal window.

    Args:
        message: Content to include in the success response.
        delay_seconds: How long to wait before responding (default: 3).
    """
    started = time.strftime("%H:%M:%S")
    await asyncio.sleep(delay_seconds)
    return (
        f"[{SERVER_ID}] DELAYED SUCCESS\n"
        f"  message:  {message}\n"
        f"  delay:    {delay_seconds}s\n"
        f"  started:  {started}\n"
        f"  finished: {time.strftime('%H:%M:%S')}"
    )


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
