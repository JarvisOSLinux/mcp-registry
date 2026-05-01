"""Identity MCP Server — every response identifies the server clearly.

Use this to confirm server discovery, tool routing, and LLM server awareness.
"""

import time

from mcp.server.fastmcp import FastMCP

SERVER_ID = "identity-mcp"
VERSION = "1.0.0"
mcp = FastMCP(SERVER_ID)


def _stamp() -> str:
    return f"[{SERVER_ID} v{VERSION}] {time.strftime('%H:%M:%S')}"


@mcp.tool()
async def whoami() -> str:
    """Return server identity, version, and current timestamp.

    Use at the start of a session to confirm which server was discovered
    and that the LLM knows it is talking to identity-mcp.
    """
    return (
        f"{_stamp()}\n"
        f"  server_id: {SERVER_ID}\n"
        f"  version:   {VERSION}\n"
        f"  transport: stdio"
    )


@mcp.tool()
async def echo(message: str) -> str:
    """Echo a message back with the server identity prefix.

    Confirms that parameter passing and tool routing are working
    end-to-end through dmcp and dispatch.

    Args:
        message: Any text to echo back.
    """
    return f"{_stamp()} ECHO: {message}"


@mcp.tool()
async def ping() -> str:
    """Instant liveness check.

    Dispatch alongside slow_echo (from slow-mcp) to observe a fast EXIT
    and a delayed EXIT in the same signal window.
    """
    return f"{_stamp()} pong"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
