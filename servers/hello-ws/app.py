"""Minimal Hello World MCP server over WebSocket. Run: uvicorn app:app --host 127.0.0.1 --port 8001"""
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from mcp.server.lowlevel import Server
from mcp.server.websocket import websocket_server
from mcp.types import Tool, TextContent

mcp_server = Server("hello-ws")

HELLO_TOOL = Tool(
    name="hello",
    description="Say hello world",
    inputSchema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Name to greet", "default": "world"}},
        "required": [],
    },
)


@mcp_server.list_tools()
async def list_tools():
    return [HELLO_TOOL]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "hello":
        n = (arguments or {}).get("name", "world")
        return [TextContent(type="text", text=f"Hello, {n}!")]
    raise ValueError(f"Unknown tool: {name}")


async def handle_websocket(websocket):
    async with websocket_server(websocket.scope, websocket.receive, websocket.send) as (read_stream, write_stream):
        init_opts = mcp_server.create_initialization_options()
        await mcp_server.run(read_stream, write_stream, init_opts)


routes = [
    WebSocketRoute("/ws", handle_websocket),
]
app = Starlette(routes=routes)
