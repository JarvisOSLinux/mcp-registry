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
from typing import Callable, Iterable, Optional


class ToolError(Exception):
    """A local tool failed; the message is safe to show the model."""


def run_proxy(
    *,
    upstream_argv: list[str],
    upstream_env: dict,
    upstream_cwd: str | None,
    local_tools: list[dict],
    handlers: dict[str, Callable[[dict], str]],
    guard: Optional[Callable[[str, dict], Optional[str]]] = None,
    hidden_tools: Iterable[str] = (),
    allowed_tools: Optional[Iterable[str]] = None,
) -> int:
    """Run until the client hangs up or the upstream exits. Returns an exit code.

    `guard(name, args)` may return an error text to refuse a forwarded call
    before it reaches the upstream (for example "not signed in").

    `hidden_tools` are upstream tools the wrapper withholds: they are dropped
    from every tools/list page and a call to one is refused here, so the client
    never learns of or reaches them. An upstream tool with the same name as a
    local one is dropped too: the wrapper's version replaces it.

    `allowed_tools`, when given, is the only upstream tools passed on (the
    manifest's reviewed list, `reviewed.declared_tools`): anything else the
    upstream offers is withheld the same way.
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

    hidden = set(hidden_tools)
    allowed = None if allowed_tools is None else set(allowed_tools)
    local_names = {tool["name"] for tool in local_tools}

    def withheld_call(name) -> bool:
        return name in hidden or (allowed is not None and name not in allowed)
    out_lock = threading.Lock()
    list_ids: set = set()
    ids_lock = threading.Lock()

    def emit(obj_or_line) -> None:
        line = obj_or_line if isinstance(obj_or_line, str) else json.dumps(obj_or_line, separators=(",", ":"))
        with out_lock:
            sys.stdout.write(line.rstrip("\n") + "\n")
            sys.stdout.flush()

    def reply_tool(msg_id, text: str, is_error: bool = False) -> None:
        result = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        emit({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def run_local(msg_id, name: str, args: dict) -> None:
        try:
            reply_tool(msg_id, handlers[name](args))
        except ToolError as err:
            reply_tool(msg_id, str(err), True)
        except Exception as err:  # never let a handler bug kill the relay
            print(f"local tool {name!r} crashed: {err!r}", file=sys.stderr, flush=True)
            reply_tool(msg_id, f"{name} failed unexpectedly ({type(err).__name__})", True)

    def pump_upstream() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                emit(line)
                continue
            if isinstance(msg, dict) and "result" in msg and "id" in msg:
                with ids_lock:
                    is_list = msg["id"] in list_ids
                    list_ids.discard(msg["id"])
                result = msg["result"]
                if is_list and isinstance(result, dict):
                    kept = [
                        tool
                        for tool in result.get("tools", [])
                        if isinstance(tool, dict)
                        and tool.get("name") not in local_names
                        and not withheld_call(tool.get("name"))
                    ]
                    if not result.get("nextCursor"):
                        kept += local_tools
                    result["tools"] = kept
                    emit(msg)
                    continue
            emit(line)
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
            child.stdin.write(line)
            child.stdin.flush()
            continue
        method = msg.get("method") if isinstance(msg, dict) else None
        if method == "tools/list" and "id" in msg:
            with ids_lock:
                list_ids.add(msg["id"])
        elif method == "tools/call" and "id" in msg:
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name in handlers:
                threading.Thread(target=run_local, args=(msg["id"], name, args), daemon=True).start()
                continue
            if withheld_call(name):
                reply_tool(msg["id"], f"{name} is not available through this server.", True)
                continue
            refusal = guard(name, args) if guard else None
            if refusal:
                reply_tool(msg["id"], refusal, True)
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


def list_upstream_tools(
    *,
    upstream_argv: list[str],
    upstream_env: dict,
    upstream_cwd: str | None,
    timeout: float = 60,
) -> list[str]:
    """Every tool the upstream offers, nothing withheld: the drift check's view.

    Reached only through a wrapper's `--list-upstream-tools` flag, which a
    maintainer passes by hand. dmcp starts a wrapper with its manifest's
    arguments, so an agent cannot ask for this.
    """
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
    watchdog = threading.Timer(timeout, child.kill)
    watchdog.start()
    assert child.stdin is not None and child.stdout is not None

    def send(message: dict) -> None:
        child.stdin.write(json.dumps(message) + "\n")
        child.stdin.flush()

    def answer(wanted: int) -> dict:
        for line in child.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == wanted:
                if "error" in msg:
                    raise OSError(f"the upstream refused: {msg['error']}")
                return msg.get("result") or {}
        raise OSError("the upstream exited without answering")

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "tool-drift-check", "version": "1"},
                },
            }
        )
        answer(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names: list[str] = []
        cursor = None
        for request_id in range(2, 1000):
            send({"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {"cursor": cursor} if cursor else {}})
            result = answer(request_id)
            names += [t["name"] for t in result.get("tools", []) if isinstance(t, dict) and isinstance(t.get("name"), str)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return names
    finally:
        watchdog.cancel()
        child.kill()
        child.wait()
