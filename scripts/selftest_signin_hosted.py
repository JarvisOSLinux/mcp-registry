#!/usr/bin/env python3
"""selftest_signin_hosted.py — prove the hosted-server sign-in and its HTTP relay.

Project-JARVIS#229 (design v3): a hosted MCP server such as Notion leaves sign-in
to the client, so its wrapper runs the browser OAuth flow itself and relays calls
to the hosted server. A loopback server stands in for both the authorization
server and the MCP endpoint, and it ENFORCES what a real one does: client
registration, redirect-URI registration, PKCE S256 verification, `resource`, and
the bearer on every call.

  1. The spec flow signs in: discovery, registration, PKCE, `resource`, and the
     token exchange all reach the server, and the stored record carries what a
     refresh needs.
  2. A forged callback (wrong `state`) is ignored and does not end the sign-in.
  3. Declined consent fails cleanly; an authorization endpoint off the pinned
     host is refused before any link is shown.
  4. Refresh works for the same `resource`, keeps a refresh token the server does
     not rotate, and fails without one.
  5. The HTTP relay answers initialize and tools/list itself while signed out
     (so `login` is reachable), refuses other calls with "not signed in",
     connects the hosted server on first need, replays the client's initialize,
     delivers server-sent events as they arrive, and retries once after a 401
     with a refreshed token.
  6. The Notion wrapper's token source renews an expired token on use.

Offline, stdlib only; the token store is forced to a temp-dir file.
"""

import base64
import contextlib
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "servers"
# Last inserted wins: servers/ must come first so `_lib` is the canonical source,
# not the wrapper's vendored copy (selftest_signin_lib.py checks that copy).
sys.path.insert(0, str(SERVERS / "notion"))
sys.path.insert(0, str(SERVERS))

from _lib import oauthbrowser, tokenstore  # noqa: E402
from _lib.devicecode import SignInError  # noqa: E402

FAILURES: list = []
running = ""


def check(ok: bool, what: str) -> None:
    if not ok:
        FAILURES.append(f"{running}: {what}")
        print(f"    FAIL: {what}")


class Hosted:
    """Authorization server and MCP endpoint in one loopback server."""

    def __init__(self):
        self.clients: dict = {}
        self.codes: dict = {}
        self.valid_tokens: set = set()
        self.refresh_tokens: set = set()
        self.verified_pkce = False
        self.saw_resource: list = []
        self.deny = False
        self.rotate_refresh = True
        self.requests: list = []  # (method-or-path, headers dict, body)
        self.authorization_endpoint = None
        hosted = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _send(self, code, body=b"", ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj, extra=None):
                self._send(code, json.dumps(obj).encode(), extra=extra)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
                base = hosted.base
                if parts.path == "/.well-known/oauth-protected-resource/mcp":
                    self._json(200, {"resource": f"{base}/mcp", "authorization_servers": [base]})
                elif parts.path == "/.well-known/oauth-authorization-server":
                    self._json(200, {
                        "issuer": base,
                        "authorization_endpoint": hosted.authorization_endpoint or f"{base}/authorize",
                        "token_endpoint": f"{base}/token",
                        "registration_endpoint": f"{base}/register",
                        "code_challenge_methods_supported": ["S256"],
                        "scopes_supported": ["default"],
                    })
                elif parts.path == "/authorize":
                    client = hosted.clients.get(query.get("client_id"))
                    ok = (
                        client is not None
                        and query.get("redirect_uri") in client["redirect_uris"]
                        and query.get("code_challenge_method") == "S256"
                        and query.get("response_type") == "code"
                        and query.get("code_challenge")
                    )
                    if not ok:
                        self._json(400, {"error": "invalid_request"})
                        return
                    hosted.saw_resource.append(query.get("resource"))
                    if hosted.deny:
                        target = {"error": "access_denied", "state": query["state"]}
                    else:
                        code = f"code{len(hosted.codes)}"
                        hosted.codes[code] = {
                            "challenge": query["code_challenge"],
                            "redirect_uri": query["redirect_uri"],
                            "client_id": query["client_id"],
                            "resource": query.get("resource"),
                        }
                        target = {"code": code, "state": query["state"]}
                    self._send(302, extra={"Location": f"{query['redirect_uri']}?{urllib.parse.urlencode(target)}"})
                else:
                    self._json(404, {})

            def do_POST(self):
                body = self._body()
                path = self.path
                hosted.requests.append((path, dict(self.headers), body))
                if path == "/register":
                    reg = json.loads(body)
                    client_id = f"client{len(hosted.clients)}"
                    hosted.clients[client_id] = {"redirect_uris": reg["redirect_uris"]}
                    self._json(201, {"client_id": client_id, "token_endpoint_auth_method": "none"})
                elif path == "/token":
                    form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
                    if form.get("grant_type") == "authorization_code":
                        grant = hosted.codes.pop(form.get("code"), None)
                        verifier = form.get("code_verifier", "")
                        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                        if not grant or grant["challenge"] != digest or grant["redirect_uri"] != form.get("redirect_uri") \
                                or grant["client_id"] != form.get("client_id") or form.get("resource") != grant["resource"]:
                            self._json(400, {"error": "invalid_grant"})
                            return
                        hosted.verified_pkce = True
                        token, refresh = "at1", "rt1"
                    elif form.get("grant_type") == "refresh_token":
                        if form.get("refresh_token") not in hosted.refresh_tokens or not form.get("resource"):
                            self._json(400, {"error": "invalid_grant"})
                            return
                        token = f"at{len(hosted.valid_tokens) + 1}"
                        refresh = f"rt{len(hosted.refresh_tokens) + 1}" if hosted.rotate_refresh else None
                    else:
                        self._json(400, {"error": "unsupported_grant_type"})
                        return
                    hosted.valid_tokens.add(token)
                    reply = {"access_token": token, "token_type": "Bearer", "expires_in": 3600}
                    if refresh:
                        hosted.refresh_tokens.add(refresh)
                        reply["refresh_token"] = refresh
                    self._json(200, reply)
                elif path == "/mcp":
                    self._mcp(json.loads(body))
                else:
                    self._json(404, {})

            def _mcp(self, msg):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in hosted.valid_tokens:
                    self._send(401, extra={"WWW-Authenticate": 'Bearer resource_metadata="x"'})
                    return
                method, mid = msg.get("method"), msg.get("id")
                if method == "initialize":
                    self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {
                        "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "hosted-fake", "version": "1"}}}, extra={"Mcp-Session-Id": "sess1"})
                elif self.headers.get("Mcp-Session-Id") != "sess1":
                    self._json(400, {"error": "no session"})
                elif method is None or (mid is None and method):
                    self._send(202)
                elif method == "tools/list":
                    self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "hosted_tool"}]}})
                elif method == "tools/call":
                    note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
                    result = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "hosted:" + auth[7:]}]}}
                    stream = f"data: {json.dumps(note)}\n\n" + f"data: {json.dumps(result)}\n\n"
                    self._send(200, stream.encode(), ctype="text/event-stream")
                else:
                    self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@contextlib.contextmanager
def hosted():
    server = Hosted()
    try:
        yield server
    finally:
        server.close()


@contextlib.contextmanager
def file_store():
    names = ("JARVIS_SIGNIN_STORE", "XDG_DATA_HOME", "JARVIS_SIGNIN_NO_BROWSER")
    saved = {k: os.environ.get(k) for k in names}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update({"JARVIS_SIGNIN_STORE": "file", "XDG_DATA_HOME": tmp, "JARVIS_SIGNIN_NO_BROWSER": "1"})
        try:
            yield Path(tmp)
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)


def browser(links: list, forge: bool = False):
    """A `log` callback that plays the user's browser when it sees the sign-in link."""

    def log(message: str) -> None:
        if "http" not in message:
            return
        url = message[message.index("http"):]
        links.append(url)

        def visit():
            if forge:
                redirect = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["redirect_uri"][0]
                for bad in (f"{redirect}?code=evil&state=wrong", f"{redirect.replace('/callback', '/other')}?state=x"):
                    try:
                        urllib.request.urlopen(bad, timeout=5)
                    except Exception:
                        pass
            try:
                urllib.request.urlopen(url, timeout=10).read()
            except Exception:
                pass

        threading.Thread(target=visit, daemon=True).start()

    return log


def sign_in(server: Hosted, **kwargs) -> dict:
    links: list = []
    return oauthbrowser.login(
        resource=f"{server.base}/mcp",
        hosts=("127.0.0.1",),
        log=browser(links, kwargs.pop("forge", False)),
        open_browser=False,
        timeout=15,
        **kwargs,
    )


# -- cases --------------------------------------------------------------------


def case_login_flow():
    with hosted() as server:
        record = sign_in(server, forge=True)
        check(record["access_token"] == "at1" and record["refresh_token"] == "rt1", "the token exchange completes")
        check(server.verified_pkce, "the server verified PKCE S256")
        check(server.saw_resource == [f"{server.base}/mcp"], "`resource` is sent on authorize")
        check(len(server.clients) == 1, "the client registered itself once")
        check(record["client_id"] == "client0" and record["token_endpoint"].endswith("/token"), "the record carries what a refresh needs")
        check(record["resource"] == f"{server.base}/mcp", "the record keeps its resource")
        check(0 < record["expires_at"] - time.time() <= 3600, "expires_at is absolute")
        check(not oauthbrowser.expired(record), "a fresh token is not expired")
        check(oauthbrowser.expired({**record, "expires_at": int(time.time()) + 30}), "a token within the skew counts as expired")


def case_declined_and_off_pin():
    with hosted() as server:
        server.deny = True
        try:
            sign_in(server)
            check(False, "declined consent must fail")
        except SignInError as err:
            check("declined" in str(err), f"declined reads as declined (got {err})")
        check(not server.codes and not server.verified_pkce, "nothing was issued")

    with hosted() as server:
        server.authorization_endpoint = "https://evil.example/authorize"
        links: list = []
        try:
            oauthbrowser.login(resource=f"{server.base}/mcp", hosts=("127.0.0.1",), log=browser(links), open_browser=False, timeout=5)
            check(False, "an authorization endpoint off the pin must be refused")
        except SignInError as err:
            check("refusing" in str(err), f"the refusal is explained (got {err})")
        check(links == [], "no link is shown for a refused endpoint")


def case_refresh():
    with hosted() as server:
        record = sign_in(server)
        fresh = oauthbrowser.refresh(record, ("127.0.0.1",))
        check(fresh["access_token"] != record["access_token"], "refresh issues a new access token")
        check(fresh["refresh_token"] == "rt2", "a rotated refresh token is kept")
        check(fresh["resource"] == record["resource"] and fresh["client_id"] == record["client_id"], "refresh keeps resource and client")
        server.rotate_refresh = False
        again = oauthbrowser.refresh(fresh, ("127.0.0.1",))
        check(again["refresh_token"] == fresh["refresh_token"], "a refresh token the server does not rotate is kept")
        try:
            oauthbrowser.refresh({**record, "refresh_token": ""}, ("127.0.0.1",))
            check(False, "refresh without a refresh token must fail")
        except SignInError:
            pass


DRIVER = r'''
import sys
sys.path.insert(0, {servers!r})
from _lib import httpproxy, proxy
from pathlib import Path
TOKEN = Path({token!r})
class Tokens:
    def token(self):
        t = TOKEN.read_text().strip() if TOKEN.exists() else ""
        return t or None
    def refresh_after_401(self):
        r = Path({token!r} + ".renewed")
        if not r.exists():
            return None
        TOKEN.write_text(r.read_text())
        return r.read_text().strip()
def login(args):
    return "logged in"
sys.exit(httpproxy.run_http_proxy(
    endpoint={endpoint!r}, hosts=("127.0.0.1",), tokens=Tokens(), server_name="fake-wrapper",
    local_tools=[{{"name": "login"}}], handlers={{"login": login}}, not_signed_in="not signed in", guard=None))
'''


class Client:
    def __init__(self, driver: Path):
        self.child = subprocess.Popen([sys.executable, str(driver)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True)
        self.replies: "queue.Queue" = queue.Queue()
        threading.Thread(target=lambda: [self.replies.put(json.loads(l)) for l in self.child.stdout], daemon=True).start()

    def send(self, obj):
        self.child.stdin.write(json.dumps(obj) + "\n")
        self.child.stdin.flush()

    def get(self, timeout=8):
        try:
            return self.replies.get(timeout=timeout)
        except queue.Empty:
            return None

    def ask(self, obj, timeout=8):
        self.send(obj)
        return self.get(timeout)

    def call(self, obj, timeout=8):
        """Send a request; return (its response, every other message seen first)."""
        self.send(obj)
        others = []
        while True:
            msg = self.get(timeout)
            if msg is None:
                return None, others
            if msg.get("id") == obj["id"]:
                return msg, others
            others.append(msg)

    def close(self):
        self.child.stdin.close()
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.child.kill()


def text_of(reply) -> str:
    return reply["result"]["content"][0]["text"] if reply and "result" in reply else ""


def case_http_relay():
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    with hosted() as server, tempfile.TemporaryDirectory() as tmp:
        token_file = Path(tmp) / "token"
        driver = Path(tmp) / "driver.py"
        driver.write_text(DRIVER.format(servers=str(SERVERS), token=str(token_file), endpoint=f"{server.base}/mcp"))

        # --- signed out -------------------------------------------------------
        client = Client(driver)
        reply = client.ask(init)
        check(reply and reply["result"]["serverInfo"]["name"] == "fake-wrapper", "signed out: initialize is answered locally")
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listing = client.ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        check(listing and [t["name"] for t in listing["result"]["tools"]] == ["login"], "signed out: tools/list shows login so a user can sign in")
        refused = client.ask({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hosted_tool", "arguments": {}}})
        check(refused and refused["result"].get("isError") and text_of(refused) == "not signed in", "signed out: other calls say not signed in")
        pong = client.ask({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        check(pong and pong["result"] == {}, "signed out: ping is answered")
        check(server.requests == [], "signed out: nothing reached the hosted server")

        # --- sign in mid-session: the client's initialize is replayed ----------
        server.valid_tokens.add("good")
        token_file.write_text("good")
        listing = client.ask({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        names = [t["name"] for t in listing["result"]["tools"]] if listing else []
        check(names == ["hosted_tool", "login"], f"signed in: hosted and local tools are merged (got {names})")
        sent = [json.loads(b) for p, _h, b in server.requests if p == "/mcp"]
        check(sent and sent[0]["method"] == "initialize" and sent[0]["id"] == "wrapper-init", "the client's initialize is replayed upstream")
        check(any(m.get("method") == "notifications/initialized" for m in sent), "the initialized notification follows")
        headers = [h for p, h, b in server.requests if p == "/mcp"]
        check(all(h.get("Authorization") == "Bearer good" for h in headers), "every hosted call carries the bearer")
        check(any(h.get("Mcp-Session-Id") == "sess1" for h in headers), "the session id is echoed back")

        called, before = client.call({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "hosted_tool", "arguments": {}}})
        check(text_of(called) == "hosted:good", "a hosted call returns its result")
        check([m.get("method") for m in before] == ["notifications/progress"], "a server-sent notification arrives before the result")

        # --- 401 then a refreshed token ----------------------------------------
        server.valid_tokens.discard("good")
        server.valid_tokens.add("renewed")
        Path(str(token_file) + ".renewed").write_text("renewed")
        retried, _ = client.call({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "hosted_tool", "arguments": {}}})
        check(text_of(retried) == "hosted:renewed", "a 401 is retried once with the refreshed token")
        client.close()

        # --- signed in from the start: initialize is forwarded -----------------
        server.requests.clear()
        client = Client(driver)
        reply = client.ask(init)
        check(reply and reply["result"]["serverInfo"]["name"] == "hosted-fake", "signed in: initialize reaches the hosted server")
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.5)
        sent = [json.loads(b) for p, _h, b in server.requests if p == "/mcp"]
        check([m.get("method") for m in sent] == ["initialize", "notifications/initialized"], "initialize then initialized, once each")
        client.close()


def case_notion_token_source():
    import server as notion  # servers/notion/server.py

    with file_store(), hosted() as fake:
        saved = notion.ALLOWED_HOSTS
        notion.ALLOWED_HOSTS = ("127.0.0.1",)
        try:
            tokens = notion.StoredTokens()
            check(tokens.token() is None, "no token when signed out")
            check(tokens.refresh_after_401() is None, "nothing to refresh when signed out")
            check("Not signed in" in notion.tool_status({}), "status says not signed in")

            record = sign_in(fake)
            tokenstore.TokenStore("notion").save(record)
            check(tokens.token() == "at1", "a live token is returned as is")
            check("Signed in" in notion.tool_status({}) and "at1" not in notion.tool_status({}), "status never shows the token")

            tokenstore.TokenStore("notion").save({**record, "expires_at": int(time.time()) - 5})
            renewed = tokens.token()
            check(renewed and renewed != "at1", "an expired token is renewed on use")
            check(tokenstore.TokenStore("notion").load()["access_token"] == renewed, "the renewed token is stored")
            check(tokens.refresh_after_401() not in (None, renewed), "a 401 forces another renewal")

            notion.tool_logout({})
            check(tokens.token() is None, "logout forgets the sign-in")
        finally:
            notion.ALLOWED_HOSTS = saved


CASES = [case_login_flow, case_declined_and_off_pin, case_refresh, case_http_relay, case_notion_token_source]


def main() -> int:
    global running

    for fn in CASES:
        running = fn.__name__
        print(f"  {running}")
        try:
            fn()
        except Exception as err:
            check(False, f"crashed: {type(err).__name__}: {err}")

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} hosted sign-in self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: hosted sign-in self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
