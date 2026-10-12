"""A stdio MCP proxy that adds a wrapper's own tools to an upstream server.

The wrapper spawns the real server, relays newline-delimited JSON-RPC in both
directions, and handles a few tools itself (login, status, logout). Local tools
run on their own threads, because a login blocks for minutes and the relay must
keep moving. The upstream's stderr is inherited, so dmcp relays it live.
"""

import json
import os
import subprocess
import sys
import threading
from typing import Callable, Optional


class ToolError(Exception):
    """A local tool failed; the message is safe to show the model."""


class LocalLayer:
    """The wrapper's own tools, shared by the stdio and HTTP proxies.

    `intercept` takes each client message and says whether the layer consumed it
    (a local tool call, or a forwarded call the guard refused). `augment` adds
    the local tools to the final page of a tools/list response.
    """

    def __init__(self, local_tools, handlers, guard, emit):
        self.local_tools = local_tools
        self.handlers = handlers
        self.guard = guard
        self.emit = emit
        self._list_ids: set = set()
        self._lock = threading.Lock()

    def reply_tool(self, msg_id, text: str, is_error: bool = False) -> None:
        result = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        self.emit({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _run_local(self, msg_id, name: str, args: dict) -> None:
        try:
            self.reply_tool(msg_id, self.handlers[name](args))
        except ToolError as err:
            self.reply_tool(msg_id, str(err), True)
        except Exception as err:  # never let a handler bug kill the relay
            print(f"local tool {name!r} crashed: {err!r}", file=sys.stderr, flush=True)
            self.reply_tool(msg_id, f"{name} failed unexpectedly ({type(err).__name__})", True)

    def local_list(self, msg_id) -> None:
        """Answer tools/list with the local tools alone (upstream unavailable)."""
        self.emit({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": list(self.local_tools)}})

    def intercept(self, msg) -> bool:
        if not isinstance(msg, dict) or "id" not in msg:
            return False
        method = msg.get("method")
        if method == "tools/list":
            with self._lock:
                self._list_ids.add(msg["id"])
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name in self.handlers:
                threading.Thread(target=self._run_local, args=(msg["id"], name, args), daemon=True).start()
                return True
            refusal = self.guard(name, args) if self.guard else None
            if refusal:
                self.reply_tool(msg["id"], refusal, True)
                return True
        return False

    def forget_list(self, msg_id) -> None:
        with self._lock:
            self._list_ids.discard(msg_id)

    def augment(self, msg):
        """Return `msg` with the local tools added if it answers a tools/list, else None."""
        if not (isinstance(msg, dict) and "result" in msg and "id" in msg):
            return None
        with self._lock:
            if msg["id"] not in self._list_ids:
                return None
            self._list_ids.discard(msg["id"])
        result = msg["result"]
        if isinstance(result, dict) and not result.get("nextCursor"):
            result["tools"] = [*result.get("tools", []), *self.local_tools]
            return msg
        return None


def make_emitter():
    lock = threading.Lock()

    def emit(obj_or_line) -> None:
        line = obj_or_line if isinstance(obj_or_line, str) else json.dumps(obj_or_line, separators=(",", ":"))
        with lock:
            sys.stdout.write(line.rstrip("\n") + "\n")
            sys.stdout.flush()

    return emit


def run_proxy(
    *,
    upstream_argv: list[str],
    upstream_env: dict,
    upstream_cwd: str | None,
    local_tools: list[dict],
    handlers: dict[str, Callable[[dict], str]],
    guard: Optional[Callable[[str, dict], Optional[str]]] = None,
) -> int:
    """Run until the client hangs up or the upstream exits. Returns an exit code.

    `guard(name, args)` may return an error text to refuse a forwarded call
    before it reaches the upstream (for example "not signed in").
    """
    try:
        child = subprocess.Popen(
            upstream_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=upstream_env,
            cwd=upstream_cwd,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError as err:
        print(f"cannot start the upstream server: {err}", file=sys.stderr, flush=True)
        return 1

    emit = make_emitter()
    layer = LocalLayer(local_tools, handlers, guard, emit)

    def pump_upstream() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                emit(line)
                continue
            emit(layer.augment(msg) or line)
        code = child.wait()
        os._exit(code)  # upstream gone: the client's stdin read cannot be interrupted

    threading.Thread(target=pump_upstream, daemon=True).start()
    sys.stdin.reconfigure(encoding="utf-8")

    assert child.stdin is not None
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            msg = None
        if layer.intercept(msg):
            continue
        try:
            child.stdin.write(line if line.endswith("\n") else line + "\n")
            child.stdin.flush()
        except (BrokenPipeError, ValueError):
            break

    try:
        child.stdin.close()
    except OSError:
        pass
    try:
        return child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.terminate()
        return child.wait()
