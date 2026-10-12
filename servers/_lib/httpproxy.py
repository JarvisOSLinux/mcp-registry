"""A stdio MCP server that fronts a hosted Streamable HTTP MCP server.

dmcp speaks stdio to a wrapper; the hosted server speaks HTTP. This relays
between them, adds the wrapper's own tools (login, status, logout), and sends the
stored token as a bearer. The wrapper has to be reachable *before* sign-in, since
the login tool is how a user gets signed in: while there is no token it answers
`initialize` and `tools/list` itself, then connects the upstream (replaying the
client's initialize) the first time a call needs it.

Every request is host pinned. A 401 triggers one token refresh and one retry.
"""

import json
import sys
import threading
import urllib.request
from contextlib import closing
from typing import Callable, Optional, Protocol

from . import hostpin
from .proxy import LocalLayer, make_emitter

PROTOCOL_VERSION = "2025-06-18"
MAX_BODY_BYTES = 16 << 20
JOIN_SECONDS = 30


class Unauthorized(Exception):
    """No usable token, or the server rejected it twice."""


class UpstreamError(Exception):
    """The hosted server answered with something other than success."""


class TokenSource(Protocol):
    def token(self) -> Optional[str]: ...

    def refresh_after_401(self) -> Optional[str]: ...


def sse_messages(stream):
    """Yield each JSON message from a text/event-stream body."""
    data: list[str] = []
    for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data:
                try:
                    yield json.loads("\n".join(data))
                except json.JSONDecodeError:
                    pass
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        try:
            yield json.loads("\n".join(data))
        except json.JSONDecodeError:
            pass


class HttpUpstream:
    def __init__(self, endpoint: str, hosts, tokens: TokenSource):
        self.endpoint = hostpin.check_url(endpoint, hosts)
        self.hosts = tuple(hosts)
        self.tokens = tokens
        self.session_id: str | None = None
        self.protocol = PROTOCOL_VERSION
        self.ready = False
        self.init_msg: dict | None = None
        self._ready_lock = threading.Lock()

    def _request(self, msg: dict, token: str) -> urllib.request.Request:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "User-Agent": hostpin.USER_AGENT,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.ready:
            headers["MCP-Protocol-Version"] = self.protocol
        return urllib.request.Request(
            self.endpoint, data=json.dumps(msg).encode("utf-8"), method="POST", headers=headers
        )

    def send(self, msg: dict, on_message: Callable[[dict], None]) -> None:
        """POST one message, passing every reply message to `on_message` as it arrives."""
        status = headers = resp = None
        for attempt in (0, 1):
            token = self.tokens.token() if attempt == 0 else self.tokens.refresh_after_401()
            if not token:
                raise Unauthorized()
            status, headers, resp = hostpin.open_stream(self._request(msg, token), self.hosts)
            if status != 401:
                break
            resp.close()
        else:
            raise Unauthorized()

        with closing(resp):
            session = headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            if status == 202:
                return
            if status == 404 and self.session_id:
                self.session_id, self.ready = None, False
                raise UpstreamError("the hosted session expired; retry")
            if status != 200:
                raise UpstreamError(f"the hosted server answered HTTP {status}")
            if headers.get_content_type() == "text/event-stream":
                for item in sse_messages(resp):
                    on_message(item)
                return
            body = resp.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise UpstreamError("the hosted server's reply was too large")
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as err:
                raise UpstreamError("the hosted server's reply was not JSON") from err
            for item in parsed if isinstance(parsed, list) else [parsed]:
                on_message(item)

    def connect(self, init_msg: dict, on_message: Callable[[dict], None]) -> None:
        """Run the initialize handshake with the client's own message."""
        self.init_msg = init_msg

        def note(item):
            result = item.get("result") if isinstance(item, dict) else None
            if isinstance(result, dict) and result.get("protocolVersion"):
                self.protocol = result["protocolVersion"]
                # Ready before the client sees the reply: its `initialized`
                # notification can arrive the instant it does.
                self.ready = True
            on_message(item)

        self.send(init_msg, note)

    def ensure_ready(self) -> None:
        """Connect now, replaying the client's initialize, if nothing has yet."""
        with self._ready_lock:
            if self.ready:
                return
            if not self.init_msg:
                raise UpstreamError("the client has not initialized")
            self.connect({**self.init_msg, "id": "wrapper-init"}, lambda _item: None)
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, lambda _item: None)


def run_http_proxy(
    *,
    endpoint: str,
    hosts,
    tokens: TokenSource,
    server_name: str,
    local_tools: list,
    handlers: dict,
    guard: Optional[Callable[[str, dict], Optional[str]]] = None,
    not_signed_in: str,
) -> int:
    emit = make_emitter()
    layer = LocalLayer(local_tools, handlers, guard, emit)
    upstream = HttpUpstream(endpoint, hosts, tokens)

    def deliver(item) -> None:
        emit(layer.augment(item) or item)

    def fail(msg: dict, text: str) -> None:
        if "id" not in msg:
            return
        if msg.get("method") == "tools/call":
            layer.reply_tool(msg["id"], text, True)
        else:
            emit({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32001, "message": text}})

    def local_initialize(msg: dict) -> None:
        wanted = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        emit({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {
                "protocolVersion": wanted,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server_name, "version": "1"},
            },
        })

    def handle(msg: dict) -> None:
        method = msg.get("method")
        try:
            if method == "initialize":
                try:
                    if tokens.token():
                        upstream.connect(msg, deliver)
                        return
                except Unauthorized:
                    pass
                upstream.init_msg = msg
                local_initialize(msg)
                return
            if method is not None and "id" not in msg and not upstream.ready:
                return  # a notification for an upstream that is not connected
            if method is None and not upstream.ready:
                return  # a reply to a server request that never happened
            if method == "ping" and not upstream.ready:
                emit({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                return
            try:
                if not upstream.ready:
                    upstream.ensure_ready()
            except Unauthorized:
                if method == "tools/list":
                    layer.forget_list(msg["id"])
                    layer.local_list(msg["id"])
                else:
                    fail(msg, not_signed_in)
                return
            upstream.send(msg, deliver)
        except Unauthorized:
            fail(msg, not_signed_in)
        except (UpstreamError, OSError, hostpin.HostNotAllowed) as err:
            fail(msg, f"hosted server unavailable: {err}")
        except Exception as err:
            print(f"relay failed: {err!r}", file=sys.stderr, flush=True)
            fail(msg, f"relay failed unexpectedly ({type(err).__name__})")

    sys.stdin.reconfigure(encoding="utf-8")
    workers: list[threading.Thread] = []
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or layer.intercept(msg):
            continue
        worker = threading.Thread(target=handle, args=(msg,), daemon=True)
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join(JOIN_SECONDS)
    return 0
