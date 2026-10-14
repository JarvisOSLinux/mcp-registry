#!/usr/bin/env python3
"""selftest_signin_lib.py — prove the shared sign-in helper and the GitHub wrapper.

Project-JARVIS#229 (design v3): each wrapper owns its sign-in through the shared
helper in servers/_lib/. This self-test is what keeps that helper honest:

  1. Host pinning refuses a link off the pinned hosts, plain http, embedded
     credentials, and a redirect that leaves the pin.
  2. The device flow (RFC 8628) waits through authorization_pending, backs off
     on slow_down, and fails cleanly on access_denied and expired_token.
  3. A verification link on an unpinned host is refused before anything is shown
     or polled, and the token never appears in the stderr log line.
  4. The token store writes an owner-only file (0600 in a 0700 directory) and
     refuses to read one other users can read.
  5. The stdio proxy appends the wrapper's tools to tools/list, answers them
     locally without blocking the relay, refuses guarded calls, and passes the
     rest to the upstream with its environment.
  6. The GitHub wrapper's login never returns the token, status and logout work,
     and a hand-set token wins over a stored one.
  7. Every vendored servers/<wrapper>/_lib/ is byte-identical to servers/_lib/,
     and the drift check notices when one is not.

Offline: a loopback HTTP server stands in for GitHub. Stdlib only. Writes
nothing outside its temp directory; the token store is forced to the file
backend so the real keyring is never touched.
"""

import contextlib
import io
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ROOT / "servers"
# Last inserted wins: servers/ must come first so `_lib` is the canonical source,
# not the wrapper's vendored copy (the drift check covers that copy separately).
sys.path.insert(0, str(SERVERS / "github"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SERVERS))

from _lib import devicecode, hostpin, proxy, tokenstore  # noqa: E402
import sync_lib  # noqa: E402

FAILURES: list = []
running = ""


def check(ok: bool, what: str) -> None:
    if not ok:
        FAILURES.append(f"{running}: {what}")
        print(f"    FAIL: {what}")


# -- a loopback stand-in for GitHub -----------------------------------------


class FakeProvider:
    """Scripted device-flow provider. `polls` is the queue of token replies."""

    def __init__(self):
        self.polls: list = []
        self.requests: list = []
        self.verification_uri = None
        self.redirect_to = None
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                provider.requests.append((self.path, body))
                if self.path == "/device/code":
                    self._json(200, {
                        "device_code": "DEV123",
                        "user_code": "WDJB-MJHT",
                        "verification_uri": provider.verification_uri or f"{provider.base}/device",
                        "interval": 0,
                        "expires_in": 60,
                    })
                elif self.path == "/token":
                    reply = provider.polls.pop(0) if provider.polls else {"error": "authorization_pending"}
                    self._json(200 if "access_token" in reply else 400, reply)
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", provider.redirect_to)
                    self.end_headers()
                else:
                    self._json(404, {})

            def do_GET(self):
                provider.requests.append((self.path, self.headers.get("Authorization", "")))
                if self.path == "/user":
                    self._json(200, {"login": "octocat"})
                else:
                    self._json(404, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@contextlib.contextmanager
def provider():
    fake = FakeProvider()
    try:
        yield fake
    finally:
        fake.close()


@contextlib.contextmanager
def file_store():
    """Force the token store onto a temp-dir file backend."""
    saved = {k: os.environ.get(k) for k in ("JARVIS_SIGNIN_STORE", "XDG_DATA_HOME", "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_OAUTH_CLIENT_ID", "JARVIS_SIGNIN_NO_BROWSER")}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["JARVIS_SIGNIN_STORE"] = "file"
        os.environ["XDG_DATA_HOME"] = tmp
        os.environ["JARVIS_SIGNIN_NO_BROWSER"] = "1"
        os.environ.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)
        os.environ.pop("GITHUB_OAUTH_CLIENT_ID", None)
        try:
            yield Path(tmp)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def run_flow(fake: FakeProvider, sleeps: list, lines: list):
    return devicecode.run_device_flow(
        client_id="cid",
        scopes=["repo", "read:org"],
        device_url=f"{fake.base}/device/code",
        token_url=f"{fake.base}/token",
        allowed_hosts=("127.0.0.1",),
        log=lines.append,
        open_browser=False,
        sleep=sleeps.append,
    )


# -- cases --------------------------------------------------------------------


def case_host_pinning():
    pinned = ("github.com", "api.github.com")
    check(hostpin.check_url("https://github.com/login/device", pinned) is not None, "pinned https host accepted")
    for bad, why in (
        ("https://evil.example/login", "an unpinned host"),
        ("http://github.com/login", "plain http on a remote host"),
        ("https://user:pw@github.com/login", "embedded credentials"),
        ("javascript:alert(1)", "a non-http scheme"),
        ("https://github.com.evil.example/", "a look-alike suffix"),
    ):
        try:
            hostpin.check_url(bad, pinned)
            check(False, f"{why} must be refused")
        except hostpin.HostNotAllowed:
            pass
    check(hostpin.check_url("http://127.0.0.1:9/x", ("127.0.0.1",)) is not None, "loopback http accepted when pinned")

    with provider() as fake:
        fake.redirect_to = "https://evil.example/steal"
        try:
            hostpin.post_form(f"{fake.base}/redirect", {"a": "b"}, ("127.0.0.1",))
            check(False, "a redirect off the pin must be refused")
        except hostpin.HostNotAllowed:
            pass


def case_device_flow():
    with provider() as fake:
        fake.polls = [
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"access_token": "gho_SECRET", "token_type": "bearer", "scope": "repo"},
        ]
        sleeps: list = []
        lines: list = []
        reply = run_flow(fake, sleeps, lines)
        check(reply["access_token"] == "gho_SECRET", "flow returns the token after pending and slow_down")
        check(sleeps == [5, 5, 10], f"interval 5s, +5s after slow_down (got {sleeps})")
        check(any("WDJB-MJHT" in line and "/device" in line for line in lines), "link and code are logged")
        check(not any("gho_SECRET" in line for line in lines), "the token is never logged")
        sent = [body for path, body in fake.requests if path == "/device/code"][0]
        check("scope=repo+read%3Aorg" in sent or "scope=repo%20read%3Aorg" in sent, "scopes are sent space-separated")

    for error, needle in (("access_denied", "declined"), ("expired_token", "expired")):
        with provider() as fake:
            fake.polls = [{"error": error}]
            try:
                run_flow(fake, [], [])
                check(False, f"{error} must fail the flow")
            except devicecode.SignInError as err:
                check(needle in str(err), f"{error} reads as {needle!r} (got {err})")


def case_unpinned_verification_link():
    with provider() as fake:
        fake.verification_uri = "https://evil.example/device"
        lines: list = []
        try:
            run_flow(fake, [], lines)
            check(False, "a verification link off the pin must be refused")
        except devicecode.SignInError:
            pass
        check(lines == [], "nothing is shown for a refused link")
        check(not any(path == "/token" for path, _ in fake.requests), "no token poll after a refused link")


def case_token_store():
    with file_store() as data:
        store = tokenstore.TokenStore("github")
        check(store.load() is None, "an empty store loads None")
        where = store.save({"access_token": "t", "login": "octocat"})
        check(where == "file", "forced file backend")
        path = data / "jarvis-mcp" / "credentials" / "github.json"
        check(stat.S_IMODE(path.stat().st_mode) == 0o600, "credentials file is 0600")
        check(stat.S_IMODE(path.parent.stat().st_mode) == 0o700, "credentials directory is 0700")
        check(store.load() == {"access_token": "t", "login": "octocat"}, "record round-trips")
        os.chmod(path, 0o644)
        try:
            store.load()
            check(False, "a group/world-readable file must be refused")
        except tokenstore.StoreError:
            pass
        os.chmod(path, 0o600)
        store.delete()
        check(store.load() is None and not path.exists(), "delete removes the record")
        try:
            tokenstore.TokenStore("../escape")
            check(False, "a path-like service name must be refused")
        except ValueError:
            pass


FAKE_UPSTREAM = r'''
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-up", "version": "0"}}}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "up", "description": "upstream", "inputSchema": {"type": "object"}}]}}
    elif method == "tools/call":
        text = "up:" + os.environ.get("X_TOKEN", "none")
        out = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
    elif method == "exit":
        sys.exit(3)
    else:
        continue
    print(json.dumps(out), flush=True)
'''

DRIVER = r'''
import os, sys, time
sys.path.insert(0, {servers!r})
from _lib import proxy
def slow(args):
    time.sleep(1.0)
    return "slow done"
def boom(args):
    raise ValueError("secret detail")
def refuse(args):
    raise proxy.ToolError("nope")
sys.exit(proxy.run_proxy(
    upstream_argv=[sys.executable, {upstream!r}],
    upstream_env={{**os.environ, "X_TOKEN": "t0k"}},
    upstream_cwd=None,
    local_tools=[{{"name": "slow"}}, {{"name": "boom"}}, {{"name": "refuse"}}],
    handlers={{"slow": slow, "boom": boom, "refuse": refuse}},
    guard=lambda name, args: "guarded" if name == "up2" else None,
))
'''


def case_proxy():
    with tempfile.TemporaryDirectory() as tmp:
        upstream = Path(tmp) / "up.py"
        upstream.write_text(FAKE_UPSTREAM)
        driver = Path(tmp) / "driver.py"
        driver.write_text(DRIVER.format(servers=str(SERVERS), upstream=str(upstream)))
        child = subprocess.Popen(
            [sys.executable, str(driver)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        replies: "queue.Queue" = queue.Queue()
        threading.Thread(target=lambda: [replies.put(json.loads(l)) for l in child.stdout], daemon=True).start()

        def send(obj):
            child.stdin.write(json.dumps(obj) + "\n")
            child.stdin.flush()

        def get(timeout=5):
            try:
                return replies.get(timeout=timeout)
            except queue.Empty:
                return None

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        init = get()
        check(init and init["result"]["serverInfo"]["name"] == "fake-up", "initialize is relayed to the upstream")

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listing = get()
        names = [t["name"] for t in listing["result"]["tools"]] if listing else []
        check(names == ["up", "slow", "boom", "refuse"], f"local tools are appended to tools/list (got {names})")

        started = time.monotonic()
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "slow", "arguments": {}}})
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "up", "arguments": {}}})
        first, second = get(), get()
        check(first and first["id"] == 4, "a forwarded call is answered while a local tool is still running")
        check(first and first["result"]["content"][0]["text"] == "up:t0k", "the upstream gets the wrapper's environment")
        check(second and second["id"] == 3 and second["result"]["content"][0]["text"] == "slow done", "the local tool answers when done")
        check(time.monotonic() - started >= 1.0, "the local tool really ran for its duration")

        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "boom", "arguments": {}}})
        crashed = get()
        check(crashed and crashed["result"].get("isError") is True, "a crashing local tool reports isError")
        check(crashed and "secret detail" not in json.dumps(crashed), "a crash does not leak the exception text")

        send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "refuse", "arguments": {}}})
        refused = get()
        check(refused and refused["result"]["content"][0]["text"] == "nope", "a ToolError message is shown as is")

        send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "up2", "arguments": {}}})
        guarded = get()
        check(guarded and guarded["result"]["content"][0]["text"] == "guarded" and guarded["result"].get("isError"),
              "the guard refuses a forwarded call before the upstream sees it")

        send({"jsonrpc": "2.0", "method": "exit"})
        try:
            code = child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            code = None
        check(code == 3, f"the proxy exits with the upstream's code (got {code})")


def case_github_wrapper():
    import server  # servers/github/server.py

    with file_store():
        with provider() as fake:
            saved = (server.DEVICE_URL, server.TOKEN_URL, server.IDENTITY_URL, server.ALLOWED_HOSTS)
            server.DEVICE_URL = f"{fake.base}/device/code"
            server.TOKEN_URL = f"{fake.base}/token"
            server.IDENTITY_URL = f"{fake.base}/user"
            server.ALLOWED_HOSTS = ("127.0.0.1",)
            try:
                check("Not signed in" in server.tool_status({}), "status before login says not signed in")
                check(server.guard("get_file_contents", {}) == server.NOT_SIGNED_IN, "a forwarded call is refused before login")

                try:
                    server.tool_login({})
                    check(False, "login with no client id must fail")
                except proxy.ToolError as err:
                    check("client id" in str(err), "the missing client id is named")

                os.environ["GITHUB_OAUTH_CLIENT_ID"] = "cid"
                fake.polls = [{"access_token": "gho_SECRET", "token_type": "bearer", "scope": "repo"}]
                with contextlib.redirect_stderr(io.StringIO()) as err_out:
                    result = server.tool_login({})
                check("octocat" in result, "login reports who signed in")
                check("gho_SECRET" not in result and "gho_SECRET" not in err_out.getvalue(), "the token is in neither the result nor the log")
                check(server.current_token() == "gho_SECRET", "the stored token is what the upstream runs with")
                check(server.guard("get_file_contents", {}) is None, "calls go through once signed in")
                check("octocat" in server.tool_status({}) and "gho_SECRET" not in server.tool_status({}), "status names the account, not the token")
                bearer = [auth for path, auth in fake.requests if path == "/user"]
                check(bearer == ["Bearer gho_SECRET"], "identity is fetched with the new token")

                os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = "hand-set"
                check(server.current_token() == "hand-set", "a hand-set token wins over a stored one")
                os.environ.pop("GITHUB_PERSONAL_ACCESS_TOKEN")

                server.tool_logout({})
                check(server.current_token() == "", "logout forgets the token")
            finally:
                server.DEVICE_URL, server.TOKEN_URL, server.IDENTITY_URL, server.ALLOWED_HOSTS = saved


def case_vendored_copies():
    check(sync_lib.differing(SERVERS) == [], "every vendored _lib matches servers/_lib")
    check(bool(sync_lib.vendored_copies(SERVERS)), "the GitHub wrapper carries a vendored copy")

    with tempfile.TemporaryDirectory() as tmp:
        servers = Path(tmp) / "servers"
        shutil.copytree(SERVERS / "_lib", servers / "_lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SERVERS / "_lib", servers / "w" / "_lib", ignore=shutil.ignore_patterns("__pycache__"))
        check(sync_lib.differing(servers) == [], "identical copies are clean")
        (servers / "w" / "_lib" / "hostpin.py").write_text("# tampered\n")
        check(len(sync_lib.differing(servers)) == 1, "an edited copy is reported")
        (servers / "w" / "_lib" / "hostpin.py").write_bytes((servers / "_lib" / "hostpin.py").read_bytes())
        (servers / "w" / "_lib" / "extra.py").write_text("x = 1\n")
        check(len(sync_lib.differing(servers)) == 1, "an extra file in a copy is reported")


CASES = [
    case_host_pinning,
    case_device_flow,
    case_unpinned_verification_link,
    case_token_store,
    case_proxy,
    case_github_wrapper,
    case_vendored_copies,
]


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
        print(f"\nFAIL: {len(FAILURES)} sign-in self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: sign-in self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
