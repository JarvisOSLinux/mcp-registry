"""Calculator MCP Server - provides basic arithmetic operations."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calculator-py")


@mcp.tool()
async def add(a: float, b: float) -> str:
    """Add two numbers together.

    Args:
        a: First number
        b: Second number
    """
    return str(a + b)


@mcp.tool()
async def subtract(a: float, b: float) -> str:
    """Subtract the second number from the first.

    Args:
        a: First number
        b: Second number
    """
    return str(a - b)


@mcp.tool()
async def multiply(a: float, b: float) -> str:
    """Multiply two numbers together.

    Args:
        a: First number
        b: Second number
    """
    return str(a * b)


@mcp.tool()
async def divide(a: float, b: float) -> str:
    """Divide the first number by the second.

    Args:
        a: Dividend
        b: Divisor (must not be zero)
    """
    if b == 0:
        return "Error: Division by zero"
    return str(a / b)


def main():
    """Entry point for the calculator MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
