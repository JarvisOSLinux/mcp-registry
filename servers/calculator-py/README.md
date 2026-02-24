# Calculator (Python)

Simple arithmetic calculator MCP server written in Python. Provides add, subtract, multiply, and divide tools.

Built with the official [mcp](https://pypi.org/project/mcp/) Python SDK using FastMCP.

## Setup

Setup is handled by the registry's `setupScript`, which creates a `.venv` in the install dir and installs dependencies. No manual steps needed when installing via Discover.

## Working on the repo

When developing in this directory:

1. Create a venv: `python3 -m venv .venv`
2. Activate it: `source .venv/bin/activate` (Linux/macOS)
3. Install in editable mode: `pip install -e .`
4. Run the server: `python3 src/mcp_calculator/server.py` or `mcp-calculator-py`
