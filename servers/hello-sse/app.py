"""Minimal Hello World MCP server over SSE. Run: uvicorn app:app --host 127.0.0.1 --port 8000"""
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import Response
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

mcp_server = Server("hello-sse")

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


sse = SseServerTransport("/messages/")


async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        init_opts = mcp_server.create_initialization_options()
        await mcp_server.run(read_stream, write_stream, init_opts)
    return Response()


routes = [
    Route("/sse", endpoint=handle_sse, methods=["GET"]),
    Mount("/messages/", app=sse.handle_post_message),
]
app = Starlette(routes=routes)
