"""A stdio MCP server that fronts a hosted (Streamable HTTP) MCP server.

The client talks to the wrapper over stdio, as to any local server; the wrapper
talks to the hosted server over HTTPS with the user's token. The wrapper answers
`initialize` itself and opens its own session upstream only when a request needs
it, so a wrapper that is not signed in still starts, lists its own tools, and can
run `login`.

- tools/list: the upstream's tools (withheld ones dropped, same-named local ones
  replaced) plus the wrapper's own. Not signed in, only the wrapper's own.
- tools/call: a local tool runs on its own thread (a login blocks for minutes); a
  withheld tool is refused; anything else is forwarded. A 401 renews the token
  once and retries; a second 401 asks the user to sign in again.
- Responses come back as application/json or as an SSE stream; both are read
  until the answer to this request arrives.

Every request goes through `hostpin`: the hosted server's URL is pinned, and a
redirect off it is refused.
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from typing import Callable, Iterable, Optional

from . import hostpin

REQUEST_TIMEOUT_SECONDS = 300
PROTOCOL_VERSION = "2025-06-18"


class ToolError(Exception):
    """A local tool failed; the message is safe to show the model."""


class Unauthorized(Exception):
    """The hosted server rejected the token (HTTP 401)."""


class _Upstream:
    """One MCP session with the hosted server."""

    def __init__(self, url: str, hosts: tuple, client_info: dict):
        self.url, self.hosts, self.client_info = url, hosts, client_info
        self.session_id: str | None = None
        self.protocol = PROTOCOL_VERSION
        self.next_id = 0
        self.ready = False

    def _post(self, message: dict, token: str) -> dict | None:
        hostpin.check_url(self.url, self.hosts)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": self.protocol,
            "User-Agent": hostpin.USER_AGENT,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST")
        opener = urllib.request.build_opener(hostpin._PinnedRedirects(self.hosts))
        try:
            resp = opener.open(req, timeout=REQUEST_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise Unauthorized() from err
            body = err.read(4096).decode("utf-8", "replace")
            raise OSError(f"the server answered HTTP {err.code}: {body[:200]}") from err
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            if resp.status == 202 or "id" not in message:
                return None
            kind = resp.headers.get("Content-Type", "")
            if "text/event-stream" in kind:
                return self._from_sse(resp, message["id"])
            return json.loads(resp.read(8 << 20).decode("utf-8"))

    @staticmethod
    def _from_sse(resp, wanted) -> dict | None:
        data: list = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line == "" and data:
                try:
                    event = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    event = None
                data = []
                if isinstance(event, dict) and event.get("id") == wanted and ("result" in event or "error" in event):
                    return event
        return None

    def request(self, method: str, params: dict | None, token: str) -> dict:
        if not self.ready:
            init = {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": self.client_info},
            }
            answer = self._post(init, token) or {}
            version = (answer.get("result") or {}).get("protocolVersion")
            if version:
                self.protocol = version
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token)
            self.ready = True
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": f"w{self.next_id}", "method": method}
        if params is not None:
            message["params"] = params
        answer = self._post(message, token)
        if not isinstance(answer, dict):
            raise OSError("the server sent no answer")
        return answer


def run_http_proxy(
    *,
    url: str,
    allowed_hosts: Iterable[str],
    get_token: Callable[[bool], Optional[str]],
    local_tools: list[dict],
    handlers: dict[str, Callable[[dict], str]],
    server_info: dict,
    not_signed_in: str,
    hidden_tools: Iterable[str] = (),
) -> int:
    """Serve stdio until the client hangs up.

    `get_token(renew)` returns the access token, renewing it first when `renew`
    is true (after a 401), or None when there is no sign-in.
    """
    hosts = tuple(allowed_hosts)
    hidden = set(hidden_tools)
    withheld = hidden | {tool["name"] for tool in local_tools}
    upstream = _Upstream(url, hosts, {"name": server_info.get("name", "wrapper"), "version": server_info.get("version", "1")})
    out_lock = threading.Lock()

    def emit(obj: dict) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
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
        except Exception as err:  # never let a handler bug kill the server
            print(f"local tool {name!r} crashed: {err!r}", file=sys.stderr, flush=True)
            reply_tool(msg_id, f"{name} failed unexpectedly ({type(err).__name__})", True)

    def forward(method: str, params: dict | None) -> dict:
        token = get_token(False)
        if not token:
            raise Unauthorized()
        try:
            return upstream.request(method, params, token)
        except Unauthorized:
            token = get_token(True)
            if not token:
                raise
            upstream.ready, upstream.session_id = False, None
            return upstream.request(method, params, token)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or "id" not in msg:
            continue  # notifications from the client need no answer here
        msg_id, method = msg["id"], msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if method == "initialize":
            emit({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": server_info,
            }})
        elif method == "ping":
            emit({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            try:
                answer = forward("tools/list", params)
                result = answer.get("result") or {}
                tools = [t for t in result.get("tools", []) if not (isinstance(t, dict) and t.get("name") in withheld)]
                if not result.get("nextCursor"):
                    tools += local_tools
                emit({"jsonrpc": "2.0", "id": msg_id, "result": {**result, "tools": tools}})
            except (Unauthorized, OSError, ValueError, hostpin.HostNotAllowed):
                emit({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": list(local_tools)}})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if name in handlers:
                threading.Thread(target=run_local, args=(msg_id, name, args), daemon=True).start()
            elif name in hidden:
                reply_tool(msg_id, f"{name} is not available through this server.", True)
            else:
                try:
                    answer = forward("tools/call", params)
                    emit({**answer, "id": msg_id})
                except Unauthorized:
                    reply_tool(msg_id, not_signed_in, True)
                except (OSError, ValueError, hostpin.HostNotAllowed) as err:
                    reply_tool(msg_id, f"{name} failed: {err}", True)
        else:
            try:
                answer = forward(method, params or None)
                emit({**answer, "id": msg_id})
            except Unauthorized:
                emit({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32001, "message": not_signed_in}})
            except (OSError, ValueError, hostpin.HostNotAllowed) as err:
                emit({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(err)}})
    return 0
