#!/usr/bin/env python3
"""selftest_notion.py — prove the Notion wrapper and the hosted-server helper parts.

servers/notion fronts Notion's hosted MCP server, so it is the OAuth client the
MCP authorization spec asks for. This runs the real wrapper against a fake hosted
server on loopback that enforces what a real one does — dynamic registration of
the exact redirect, PKCE S256, the RFC 8707 `resource`, a bearer on every MCP
request, refresh — and that answers some requests as an SSE stream. The test
plays the browser. No network, no Notion account.

  1. `login` follows the spec end to end, stores a record that can be renewed,
     and never shows a token.
  2. A stray or forged callback neither finishes nor aborts the sign-in; a
     declined consent and a timeout are reported, and nothing is stored.
  3. An authorization server advertising an endpoint off the pinned hosts is
     refused before anything is opened.
  4. The proxy answers initialize itself, lists only its own tools until signed
     in, refuses calls with a sign-in message, then forwards (JSON and SSE),
     renews an expiring token, recovers from a 401 once, and asks for a new
     sign-in when renewal fails.
  5. The manifest declares a blocking login with a reminder, and the
     vendored _lib is identical to servers/_lib.

JOBS_SELFTEST_TREE works as for selftest_jobs.py.

Usage:
  python3 scripts/selftest_notion.py
"""

import base64
import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TREE = Path(os.environ.get("JOBS_SELFTEST_TREE") or Path(__file__).resolve().parent.parent)
WRAPPER = TREE / "servers" / "notion"
sys.path.insert(0, str(WRAPPER))

import server  # noqa: E402  servers/notion/server.py
from _lib import httpproxy, oauthbrowser  # noqa: E402

FAILURES: list = []
running = ""


def check(ok: bool, what: str) -> None:
    print(f"    {'ok  ' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILURES.append(f"{running}: {what}")


class FakeNotion:
    """A hosted MCP server and its authorization server, on one loopback port."""

    def __init__(self):
        self.mode = "ok"
        self.log: list = []
        self.clients: dict = {}
        self.codes: dict = {}
        self.valid = {"at-1"}
        self.issued = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def send(self, status, body=None, headers=None, raw=None, kind="application/json"):
                data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", kind)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                fake.log.append({"GET": url.path, "query": q})
                base = fake.base
                if url.path.startswith("/.well-known/oauth-protected-resource"):
                    return self.send(200, {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": ["default"]})
                if url.path.startswith("/.well-known/oauth-authorization-server"):
                    token = "https://elsewhere.invalid/token" if fake.mode == "off-pin" else f"{base}/token"
                    return self.send(200, {"issuer": base, "authorization_endpoint": f"{base}/authorize",
                                           "token_endpoint": token, "registration_endpoint": f"{base}/register",
                                           "code_challenge_methods_supported": ["plain", "S256"]})
                if url.path == "/authorize":
                    client = fake.clients.get(q.get("client_id"))
                    if not client or q.get("redirect_uri") not in client or q.get("code_challenge_method") != "S256" \
                            or q.get("resource") != f"{base}/mcp":
                        return self.send(400, {"error": "invalid_request"})
                    state = urllib.parse.quote(q.get("state", ""))
                    if fake.mode == "deny":
                        location = f"{q['redirect_uri']}?error=access_denied&state={state}"
                    else:
                        fake.issued += 1
                        code = f"code-{fake.issued}"
                        fake.codes[code] = (q["code_challenge"], q["redirect_uri"], q["client_id"])
                        location = f"{q['redirect_uri']}?code={code}&state={state}"
                    return self.send(302, headers={"Location": location})
                self.send(404, {"error": "not_found"})

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                base = fake.base
                if url.path == "/register":
                    req = json.loads(raw)
                    fake.log.append({"POST": "/register", "json": req})
                    cid = f"client-{len(fake.clients) + 1}"
                    fake.clients[cid] = req.get("redirect_uris", [])
                    return self.send(201, {"client_id": cid, "redirect_uris": req.get("redirect_uris", [])})
                if url.path == "/token":
                    form = dict(urllib.parse.parse_qsl(raw))
                    fake.log.append({"POST": "/token", "form": form})
                    if form.get("resource") != f"{base}/mcp" or form.get("client_id") not in fake.clients:
                        return self.send(400, {"error": "invalid_target"})
                    if form.get("grant_type") == "authorization_code":
                        pending = fake.codes.pop(form.get("code", ""), None)
                        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                        if not pending or pending[1] != form.get("redirect_uri") or \
                                base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != pending[0]:
                            return self.send(400, {"error": "invalid_grant"})
                        return self.send(200, {"access_token": "at-1", "token_type": "Bearer", "expires_in": 3600,
                                               "refresh_token": "rt-1", "workspace_name": "Ada's Notion"})
                    if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "rt-1" and fake.mode != "no-refresh":
                        fake.valid.add("at-2")
                        return self.send(200, {"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600})
                    return self.send(400, {"error": "invalid_grant"})
                if url.path == "/mcp":
                    auth = self.headers.get("Authorization", "")
                    token = auth[7:] if auth.startswith("Bearer ") else ""
                    msg = json.loads(raw)
                    fake.log.append({"POST": "/mcp", "rpc": msg.get("method"), "token": token, "session": self.headers.get("Mcp-Session-Id")})
                    if token not in fake.valid:
                        return self.send(401, {"error": "invalid_token"},
                                         {"WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'})
                    if "id" not in msg:
                        return self.send(202)
                    if msg["method"] == "initialize":
                        return self.send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake-notion", "version": "0"}}}, {"Mcp-Session-Id": "sess-1"})
                    if msg["method"] == "tools/list":
                        tools = [{"name": "notion-search", "inputSchema": {"type": "object"}},
                                 {"name": "notion-delete-pages", "inputSchema": {"type": "object"}},
                                 {"name": "notion-fetch", "inputSchema": {"type": "object"}},
                                 {"name": "notion-check-mcp-next-steps", "inputSchema": {"type": "object"}}]
                        events = (f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {}})}\n\n"
                                  f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'tools': tools}})}\n\n")
                        return self.send(200, raw=events.encode(), kind="text/event-stream")
                    if msg["method"] == "tools/call":
                        text = f"searched with {token}"
                        return self.send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}]}})
                self.send(404, {"error": "not_found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@contextlib.contextmanager
def sandbox(mode="ok"):
    keys = ("XDG_DATA_HOME", "JARVIS_SIGNIN_STORE", "JARVIS_SIGNIN_NO_BROWSER", "JARVIS_SIGNIN_TIMEOUT")
    saved_env = {k: os.environ.get(k) for k in keys}
    saved = (server.RESOURCE, server.ALLOWED_HOSTS)
    fake = FakeNotion()
    fake.mode = mode
    with tempfile.TemporaryDirectory(prefix="notion-selftest-") as tmp:
        os.environ.update(XDG_DATA_HOME=tmp, JARVIS_SIGNIN_STORE="file", JARVIS_SIGNIN_NO_BROWSER="1", JARVIS_SIGNIN_TIMEOUT="20")
        server.RESOURCE, server.ALLOWED_HOSTS = f"{fake.base}/mcp", ("127.0.0.1",)
        try:
            yield fake, Path(tmp)
        finally:
            server.RESOURCE, server.ALLOWED_HOSTS = saved
            fake.close()
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def browse(url: str):
    """GET without following redirects. Returns (status, location, body)."""
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=10) as resp:
            return resp.status, resp.headers.get("Location"), resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Location"), err.read().decode()


def run_login():
    """Start tool_login on a thread; return (wait_for_link, result_holder, stderr)."""
    err = io.StringIO()
    holder: dict = {}

    def target():
        with contextlib.redirect_stderr(err):
            try:
                holder["text"] = server.tool_login({})
            except httpproxy.ToolError as e:
                holder["error"] = str(e)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def link():
        for _ in range(200):
            for line in err.getvalue().splitlines():
                if "To sign in, open this link" in line:
                    return line.split(": ", 1)[1].strip()
            time.sleep(0.05)
        return None

    return link, holder, err, thread


def case_login():
    with sandbox() as (fake, tmp):
        link, holder, err, thread = run_login()
        url = link()
        check(url is not None and url.startswith(f"{fake.base}/authorize?"), "the sign-in link is printed")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        check(q.get("code_challenge_method") == "S256" and q.get("resource") == f"{fake.base}/mcp" and q.get("state"),
              "the link carries PKCE S256, the resource and a state")
        registered = next(e for e in fake.log if e.get("POST") == "/register")["json"]
        check(registered["redirect_uris"][0].startswith("http://127.0.0.1:") and registered["token_endpoint_auth_method"] == "none",
              "it registers a loopback redirect as a public client")
        status, location, _ = browse(url)
        status, _, page = browse(location)
        thread.join(15)
        check(status == 200 and "Signed in" in page, "the redirect reaches the listener and the user sees 'Signed in'")
        check("Signed in to Notion (workspace Ada's Notion)" in holder.get("text", ""), "login names the workspace")
        everything = holder.get("text", "") + err.getvalue()
        check("at-1" not in everything and "rt-1" not in everything, "no token in the result or the log")
        stored = json.loads((tmp / "jarvis-mcp" / "credentials" / "notion.json").read_text())
        check(stored.get("refresh_token") == "rt-1" and stored.get("expires_at") and stored.get("client_id") == "client-1"
              and stored.get("resource") == f"{fake.base}/mcp", "the stored record can be renewed for the same resource")
        mode = stat.S_IMODE((tmp / "jarvis-mcp" / "credentials" / "notion.json").stat().st_mode)
        check(mode == 0o600, "the stored record is owner-only")
        check(server.tool_status({}) == "Signed in to Notion (workspace Ada's Notion).", "status reports the workspace")
        server.tool_logout({})
        check(server.tool_status({}).startswith("Not signed in"), "logout forgets it")


def case_callbacks():
    with sandbox() as (fake, tmp):
        link, holder, err, thread = run_login()
        url = link()
        _, location, _ = browse(url)
        listener = location.split("/callback")[0]
        strays = [f"{listener}/callback?code=forged&state=not-ours", f"{listener}/favicon.ico", f"{listener}/callback"]
        check(all(browse(s)[0] == 404 for s in strays), "stray and forged callbacks are answered 404")
        browse(location)
        thread.join(15)
        check("Signed in to Notion" in holder.get("text", ""), "and the real callback still completes the sign-in")
    with sandbox("deny") as (fake, tmp):
        link, holder, err, thread = run_login()
        _, location, _ = browse(link())
        _, _, page = browse(location)
        thread.join(15)
        check(holder.get("error") == "sign-in was declined" and "not completed" in page, "a declined consent is reported")
        check(not (tmp / "jarvis-mcp").exists(), "and nothing is stored")
    with sandbox() as (fake, tmp):
        os.environ["JARVIS_SIGNIN_TIMEOUT"] = "1"
        link, holder, err, thread = run_login()
        thread.join(15)
        check(holder.get("error") == "the sign-in was not approved in time", "an unapproved sign-in times out")


def case_pinning():
    with sandbox("off-pin") as (fake, tmp):
        opened: list = []
        saved = oauthbrowser.browser.open_link
        oauthbrowser.browser.open_link = lambda url, hosts: opened.append(url) or True
        try:
            link, holder, err, thread = run_login()
            thread.join(15)
        finally:
            oauthbrowser.browser.open_link = saved
        check("refusing the server's sign-in endpoints" in holder.get("error", ""), "an endpoint off the pinned hosts is refused")
        check(not opened and not any(e.get("POST") == "/register" for e in fake.log), "before registering or opening anything")


PROXY_MAIN = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {wrapper!r})
    import server
    server.RESOURCE, server.ALLOWED_HOSTS = {resource!r}, ("127.0.0.1",)
    sys.exit(server.main())
    """
)


class Client:
    def __init__(self, tmp: Path, resource: str):
        main = tmp / "proxy_main.py"
        main.write_text(PROXY_MAIN.format(wrapper=str(WRAPPER), resource=resource))
        self.child = subprocess.Popen([sys.executable, str(main)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, env=dict(os.environ))
        self.n = 0

    def ask(self, method, params=None):
        self.n += 1
        self.child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.child.stdin.flush()
        answer = json.loads(self.child.stdout.readline())
        assert answer["id"] == self.n, answer
        return answer

    def close(self):
        self.child.kill()
        self.child.wait()


def store(record: dict) -> None:
    server._store().save(record)


def case_proxy():
    with sandbox() as (fake, tmp):
        resource = f"{fake.base}/mcp"
        client = Client(tmp, resource)
        init = client.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        check(init["result"]["serverInfo"]["name"].startswith("notion"), "initialize is answered by the wrapper")
        names = [t["name"] for t in client.ask("tools/list")["result"]["tools"]]
        check(names == ["login", "status", "logout"], "not signed in, only the wrapper's own tools are listed")
        call = client.ask("tools/call", {"name": "notion-search", "arguments": {"query": "x"}})["result"]
        check(call.get("isError") and "Call the `login` tool" in call["content"][0]["text"], "a forwarded call asks for a sign-in")
        check(not any(e.get("POST") == "/mcp" for e in fake.log), "and nothing was sent upstream without a token")

        fake.clients["client-1"] = []
        store({"access_token": "at-1", "refresh_token": "rt-1", "client_id": "client-1", "token_endpoint": f"{fake.base}/token",
               "resource": resource, "expires_at": int(time.time()) + 3600})
        names = [t["name"] for t in client.ask("tools/list")["result"]["tools"]]
        check(names == ["notion-search", "notion-fetch", "login", "status", "logout"],
              "signed in, Notion's reviewed tools come through (SSE), withheld and unreviewed ones dropped, plus the wrapper's")
        refused = client.ask("tools/call", {"name": "notion-delete-pages", "arguments": {"page_ids": ["x"]}})["result"]
        check(refused.get("isError") and not any(e.get("rpc") == "tools/call" for e in fake.log),
              "a withheld delete is refused before anything reaches Notion")
        upsell = client.ask("tools/call", {"name": "notion-check-mcp-next-steps", "arguments": {}})["result"]
        check(upsell.get("isError") and not any(e.get("rpc") == "tools/call" for e in fake.log),
              "a tool the manifest does not declare is refused before anything reaches Notion")
        call = client.ask("tools/call", {"name": "notion-search", "arguments": {"query": "x"}})["result"]
        check(call["content"][0]["text"] == "searched with at-1", "a call is forwarded with the token (JSON)")
        sessions = {e.get("session") for e in fake.log if e.get("rpc") == "tools/call"}
        check(sessions == {"sess-1"}, "the upstream session id is carried")

        fake.valid.discard("at-1")
        call = client.ask("tools/call", {"name": "notion-search", "arguments": {}})["result"]
        check(call["content"][0]["text"] == "searched with at-2", "a 401 renews the token once and retries")
        refresh = [e for e in fake.log if e.get("form", {}).get("grant_type") == "refresh_token"]
        check(refresh and refresh[-1]["form"]["resource"] == resource, "the renewal is for the same resource")
        renewed = json.loads((tmp / "jarvis-mcp" / "credentials" / "notion.json").read_text())
        check(renewed["access_token"] == "at-2", "and the renewed token is stored")
        check(renewed.get("refresh_token") == "rt-1", "keeping the refresh token the server did not rotate")
        client.close()

    with sandbox() as (fake, tmp):
        resource = f"{fake.base}/mcp"
        fake.clients["client-1"] = []
        store({"access_token": "at-1", "refresh_token": "rt-1", "client_id": "client-1", "token_endpoint": f"{fake.base}/token",
               "resource": resource, "expires_at": int(time.time()) + 10})
        client = Client(tmp, resource)
        call = client.ask("tools/call", {"name": "notion-search", "arguments": {}})["result"]
        check(call["content"][0]["text"] == "searched with at-2", "a token about to expire is renewed before the call")
        client.close()

    with sandbox("no-refresh") as (fake, tmp):
        resource = f"{fake.base}/mcp"
        fake.clients["client-1"] = []
        fake.valid.clear()
        store({"access_token": "at-1", "refresh_token": "rt-1", "client_id": "client-1", "token_endpoint": f"{fake.base}/token",
               "resource": resource, "expires_at": int(time.time()) + 3600})
        client = Client(tmp, resource)
        call = client.ask("tools/call", {"name": "notion-search", "arguments": {}})["result"]
        check(call.get("isError") and "Call the `login` tool" in call["content"][0]["text"], "a revoked sign-in asks for a new one")
        client.close()


def case_manifest_and_vendoring():
    manifest = json.loads((WRAPPER / "manifest.json").read_text())
    declared = {t["name"]: t for t in manifest["tools"]}
    login_tool = declared.get("login", {})
    # How long the interval is belongs to validate_registry's floor, not here.
    interval = login_tool.get("suggestedRemindAfter")
    check(login_tool.get("blocking") is True and type(interval) is int and interval > 0, "login is blocking with a reminder")
    check(manifest.get("login") == {"tool": "login"}, "the manifest names its login tool")
    check(not any(t.get("threat_level") == "forbidden" for t in declared.values()), "no tool is declared forbidden")
    check({t["name"] for t in server.LOCAL_TOOLS} <= set(declared), "every local tool is declared")
    check(not (set(declared) & server.HIDDEN_TOOLS), "no withheld tool is declared")
    sys.path.insert(0, str(TREE / "scripts"))
    import sync_lib
    check(sync_lib.tree(WRAPPER / "_lib") == sync_lib.tree(TREE / "servers" / "_lib"), "the vendored _lib is identical to servers/_lib")


def main() -> int:
    global running
    for fn in (case_login, case_callbacks, case_pinning, case_proxy, case_manifest_and_vendoring):
        running = fn.__name__
        print(f"  {running}")
        fn()
    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: Notion wrapper self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
