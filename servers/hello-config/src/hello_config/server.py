"""Minimal MCP server: hello world, for testing configurableProperties and scope."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hello-config")


@mcp.tool()
async def hello(name: str = "world") -> str:
    """Say hello. Optional name (default: world)."""
    return f"Hello, {name}!"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
