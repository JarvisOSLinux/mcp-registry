#!/usr/bin/env python3
"""selftest_microsoft_365.py — prove the Microsoft 365 wrapper and the helper parts it adds.

The wrapper (servers/microsoft-365) fronts @softeria/ms-365-mcp-server and changes
three things; each is what keeps a real user safe or signed in, and each fails
silently if it breaks. This self-test runs the real wrapper code against stand-ins
(no Node, no Microsoft account, no network):

  1. cache_store.py speaks Softeria's auth-cache protocol exactly: a miss, a
     round trip of a 300 KB cache, delete, usage and payload errors, and nothing
     but the JSON answer on stdout. The file it falls back to is owner-only.
  2. `login` runs the upstream's blocking `--login` with the server's own
     arguments, relays the link and code to stderr, opens the browser on the
     pinned Microsoft host only, and reports who signed in. It skips the sign-in
     when already signed in, refuses a link off the pinned hosts, and reports a
     declined or expired sign-in instead of hanging.
  3. The proxy withholds the forbidden-class tools on every tools/list page,
     refuses calls to them before the upstream sees them, and replaces the
     upstream's own `login` with the wrapper's.
  4. browser.open_link keeps the browser off the MCP wire: an opener that writes
     to stdout does not reach the caller's stdout, and with no display or with
     JARVIS_SIGNIN_NO_BROWSER nothing is started.
  5. The manifest declares no withheld tool, declares `login` blocking with a
     reminder, and the vendored _lib is identical to servers/_lib.

JOBS_SELFTEST_TREE works as for selftest_jobs.py: the PR gate runs this script
from the base branch against the PR's tree, so the code under test is the PR's.

Usage:
  python3 scripts/selftest_microsoft_365.py
"""

import contextlib
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
from pathlib import Path

TREE = Path(os.environ.get("JOBS_SELFTEST_TREE") or Path(__file__).resolve().parent.parent)
WRAPPER = TREE / "servers" / "microsoft-365"
sys.path.insert(0, str(WRAPPER))

import server  # noqa: E402  servers/microsoft-365/server.py
from _lib import browser, proxy  # noqa: E402  the wrapper's vendored copy

FAILURES: list = []
running = ""


def check(ok: bool, what: str) -> None:
    print(f"    {'ok  ' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILURES.append(f"{running}: {what}")


@contextlib.contextmanager
def sandbox():
    """A temp data dir, the file token store, and no browser."""
    keys = ("XDG_DATA_HOME", "JARVIS_SIGNIN_STORE", "JARVIS_SIGNIN_NO_BROWSER", "MS365_PRESET", "MS365_READ_ONLY")
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory(prefix="m365-selftest-") as tmp:
        os.environ.update(XDG_DATA_HOME=tmp, JARVIS_SIGNIN_STORE="file", JARVIS_SIGNIN_NO_BROWSER="1")
        os.environ.pop("MS365_PRESET", None)
        os.environ.pop("MS365_READ_ONLY", None)
        try:
            yield Path(tmp)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def cache(*args, stdin=""):
    return subprocess.run(
        [sys.executable, str(WRAPPER / "cache_store.py"), *args],
        input=stdin, capture_output=True, text=True, env=dict(os.environ), timeout=30,
    )


def case_cache_store():
    with sandbox() as tmp:
        miss = cache("load", "token-cache")
        check(miss.returncode == 0 and json.loads(miss.stdout) == {"found": False}, "a miss is exit 0 with found:false")
        value = "envelope:" + "x" * (300 * 1024)
        saved = cache("save", "token-cache", stdin=json.dumps({"value": value}))
        check(saved.returncode == 0 and saved.stdout == "", "save commits a 300 KB cache and prints nothing")
        hit = cache("load", "token-cache")
        check(hit.returncode == 0 and json.loads(hit.stdout) == {"found": True, "value": value}, "load returns it intact")
        other = cache("load", "selected-account")
        check(json.loads(other.stdout) == {"found": False}, "the two keys are stored separately")
        stored = next((tmp / "jarvis-mcp" / "credentials").glob("microsoft-365.token-cache.json"))
        check(stat.S_IMODE(stored.stat().st_mode) == 0o600, "the fallback file is owner-only")
        gone = cache("delete", "token-cache")
        check(gone.returncode == 0 and json.loads(cache("load", "token-cache").stdout) == {"found": False}, "delete removes it")
        check(cache("delete", "token-cache").returncode == 0, "deleting what is not there is still exit 0")
        check(cache("load", "passwords").returncode == 64, "an unknown key is a usage error")
        check(cache("save", "token-cache", stdin="[1]").returncode == 65, "a malformed save payload is refused")


FAKE_UPSTREAM = textwrap.dedent(
    """
    import json, os, sys, time
    with open(os.environ["FAKE_ARGV_LOG"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    mode = os.environ.get("FAKE_MODE", "ok")
    if "--verify-login" in sys.argv:
        print(json.dumps({"success": mode == "signed-in", "userData": {"displayName": "Ada", "userPrincipalName": "ada@example.com"}}))
        sys.exit(0)
    host = "evil.example" if mode == "off-pin" else "microsoft.com"
    print()
    print(f"To sign in, use a web browser to open the page https://{host}/devicelogin and enter the code ABCD12345 to authenticate.", flush=True)
    if mode == "declined":
        sys.exit(1)
    time.sleep(30 if mode in ("slow", "off-pin") else 0.2)
    print(json.dumps({"success": True, "message": "Login successful", "userData": {"displayName": "Ada", "userPrincipalName": "ada@example.com"}}))
    """
)


@contextlib.contextmanager
def fake_upstream(tmp: Path, mode: str):
    script = tmp / "fake_upstream.py"
    script.write_text(FAKE_UPSTREAM)
    log = tmp / "argv.log"
    opened: list = []
    saved = (server.NODE, server.UPSTREAM_ENTRY, server.UPSTREAM_DIR, server.LOGIN_TIMEOUT_SECONDS, server.browser.open_link)
    os.environ.update(FAKE_ARGV_LOG=str(log), FAKE_MODE=mode)
    server.NODE, server.UPSTREAM_ENTRY, server.UPSTREAM_DIR = sys.executable, script, tmp
    server.browser.open_link = lambda url, hosts: opened.append(url) or True
    try:
        yield log, opened
    finally:
        server.NODE, server.UPSTREAM_ENTRY, server.UPSTREAM_DIR, server.LOGIN_TIMEOUT_SECONDS, server.browser.open_link = saved


def login(args=None):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return server.tool_login(args or {}), None, err.getvalue()
        except proxy.ToolError as e:
            return None, str(e), err.getvalue()


def case_login():
    with sandbox() as tmp:
        with fake_upstream(tmp, "ok") as (log, opened):
            text, error, err = login()
            check(error is None and "Signed in to Microsoft 365 as Ada (ada@example.com)" in (text or ""), "login reports who signed in")
            check("open https://microsoft.com/devicelogin and enter the code ABCD12345" in err, "the link and code go to stderr")
            check(opened == ["https://microsoft.com/devicelogin"], "the browser opens the pinned link once")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            check(calls == [["--preset", "mail,calendar", "--verify-login"], ["--preset", "mail,calendar", "--login"]],
                  "verify-login, then the blocking --login, with the server's own arguments")
        with fake_upstream(tmp, "signed-in") as (log, opened):
            log.unlink(missing_ok=True)
            text, error, _ = login()
            check("Already signed in to Microsoft 365 as Ada" in (text or ""), "an existing sign-in is reported, not repeated")
            check(len(log.read_text().splitlines()) == 1 and not opened, "and no sign-in is started")
            log.unlink()
            login({"force": True})
            check(len(log.read_text().splitlines()) == 1, "force skips the check and signs in again")
        with fake_upstream(tmp, "off-pin") as (_, opened):
            started = time.monotonic()
            text, error, _ = login()
            check(text is None and "refusing the sign-in link" in (error or ""), "a link off the pinned hosts is refused")
            check(not opened and time.monotonic() - started < 10, "nothing is opened and the upstream is stopped at once")
        with fake_upstream(tmp, "declined"):
            text, error, _ = login()
            check(text is None and "did not complete" in (error or ""), "a declined sign-in is reported")
        with fake_upstream(tmp, "slow"):
            server.LOGIN_TIMEOUT_SECONDS = 1
            started = time.monotonic()
            text, error, _ = login()
            check(error == "the sign-in code expired before it was approved" and time.monotonic() - started < 10,
                  "an expired sign-in ends at the timeout, and says it expired")


def case_account_name():
    blank = {"userData": {"displayName": " ", "userPrincipalName": "ada@example.com"}}
    check(server._account(blank) == " as ada@example.com", "a blank display name falls back to the address alone")
    both = {"userData": {"displayName": "Ada", "userPrincipalName": "ada@example.com"}}
    check(server._account(both) == " as Ada (ada@example.com)", "name and address when both exist")
    check(server._account({}) == "", "nothing when the result names no account")


def case_arguments():
    with sandbox():
        check(server.upstream_args() == ["--preset", "mail,calendar"], "mail and calendar by default")
        os.environ["MS365_PRESET"] = "all"
        check(server.upstream_args() == [], "'all' passes no preset")
        os.environ["MS365_PRESET"] = "mail,files"
        os.environ["MS365_READ_ONLY"] = "true"
        check(server.upstream_args() == ["--preset", "mail,files", "--read-only"], "presets and read-only pass through")
        os.environ["MS365_PRESET"] = "mail;rm -rf"
        try:
            server.upstream_args()
            check(False, "a malformed preset is refused")
        except proxy.ToolError:
            check(True, "a malformed preset is refused")
        check(server.upstream_env()["MS365_MCP_AUTH_CACHE_COMMAND"] == str(server.CACHE_COMMAND),
              "the upstream keeps its token cache through cache_store.py")


FAKE_MCP = textwrap.dedent(
    """
    import json, os, sys
    log = open(os.environ["FAKE_CALLS"], "a")
    pages = [["login", "list-mail-messages", "graph-batch"], ["send-mail", "delete-calendar", "unreviewed-tool"]]
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "tools/list":
            i = int((msg.get("params") or {}).get("cursor") or 0)
            result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in pages[i]]}
            if i + 1 < len(pages):
                result["nextCursor"] = str(i + 1)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
        elif msg.get("method") == "tools/call":
            log.write(msg["params"]["name"] + "\\n"); log.flush()
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "upstream"}]}}), flush=True)
    """
)

PROXY_MAIN = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {wrapper!r})
    import server
    from _lib import proxy
    sys.exit(proxy.run_proxy(upstream_argv=[sys.executable, {fake!r}], upstream_env=dict(__import__("os").environ),
                             upstream_cwd=None, local_tools=[server.LOGIN_TOOL], handlers={{"login": lambda a: "local"}},
                             hidden_tools=server.HIDDEN_TOOLS, allowed_tools=server.REVIEWED_TOOLS))
    """
)


def case_proxy():
    with sandbox() as tmp:
        fake = tmp / "fake_mcp.py"
        fake.write_text(FAKE_MCP)
        main = tmp / "proxy_main.py"
        main.write_text(PROXY_MAIN.format(wrapper=str(WRAPPER), fake=str(fake)))
        calls = tmp / "calls.log"
        calls.touch()
        child = subprocess.Popen([sys.executable, str(main)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, FAKE_CALLS=str(calls)))

        def ask(msg):
            child.stdin.write(json.dumps(msg) + "\n")
            child.stdin.flush()
            return json.loads(child.stdout.readline())

        first = ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})["result"]
        second = ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": first.get("nextCursor")}})["result"]
        names = [t["name"] for t in first["tools"] + second["tools"]]
        check("graph-batch" not in names and "delete-calendar" not in names, "withheld tools are dropped from every page")
        check(names.count("login") == 1 and names[-1] == "login", "the upstream's login is replaced by the wrapper's, listed once")
        check({"list-mail-messages", "send-mail"} <= set(names), "reviewed tools are listed as the upstream lists them")
        check("unreviewed-tool" not in names, "a tool the manifest does not declare is dropped")
        refused = ask({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "graph-batch", "arguments": {}}})["result"]
        local = ask({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "login", "arguments": {}}})["result"]
        passed = ask({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "send-mail", "arguments": {}}})["result"]
        unreviewed = ask({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "unreviewed-tool", "arguments": {}}})["result"]
        child.kill()
        child.wait()
        check(refused.get("isError") is True, "a call to a withheld tool is refused")
        check(local["content"][0]["text"] == "local", "login is answered by the wrapper")
        check(passed["content"][0]["text"] == "upstream", "other calls reach the upstream")
        check(unreviewed.get("isError") is True, "a call to an undeclared tool is refused")
        check(calls.read_text().split() == ["send-mail"], "neither a withheld or undeclared tool nor login reached the upstream")


OPENER = "#!/bin/sh\necho 'Opening in existing browser session.'\necho \"$1\" >> \"$FAKE_OPENED\"\n"

PROBE = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {wrapper!r})
    from _lib import browser
    print("started" if browser.open_link("https://microsoft.com/devicelogin", ("microsoft.com",)) else "skipped", flush=True)
    time.sleep(0.5)
    """
)


def case_browser():
    if os.name != "posix" or sys.platform == "darwin":
        print("    skip  (xdg-open path only)")
        return
    with sandbox() as tmp:
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        opener = bin_dir / "xdg-open"
        opener.write_text(OPENER)
        opener.chmod(0o755)
        probe = tmp / "probe.py"
        probe.write_text(PROBE.format(wrapper=str(WRAPPER)))
        opened = tmp / "opened.log"
        base = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "JARVIS_SIGNIN_NO_BROWSER")}
        base.update(PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", FAKE_OPENED=str(opened))
        run = lambda extra: subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, env={**base, **extra}, timeout=30)
        shown = run({"DISPLAY": ":99"})
        check(shown.stdout == "started\n", "with a display the opener starts, and nothing it prints reaches stdout")
        check(opened.read_text().strip() == "https://microsoft.com/devicelogin", "the opener got the link")
        check(run({}).stdout == "skipped\n", "with no display nothing is started (no console browser)")
        check(run({"DISPLAY": ":99", "JARVIS_SIGNIN_NO_BROWSER": "1"}).stdout == "skipped\n", "JARVIS_SIGNIN_NO_BROWSER turns it off")
        try:
            browser.open_link("https://evil.example/x", ("microsoft.com",))
            check(False, "a link off the pinned hosts raises")
        except browser.hostpin.HostNotAllowed:
            check(True, "a link off the pinned hosts raises")


def case_manifest_and_vendoring():
    manifest = json.loads((WRAPPER / "manifest.json").read_text())
    declared = {t["name"]: t for t in manifest["tools"]}
    check(not (set(declared) & server.HIDDEN_TOOLS), "no withheld tool is declared")
    check(not any(t.get("threat_level") == "forbidden" for t in declared.values()), "no tool is declared forbidden")
    login_tool = declared.get("login", {})
    # How long the interval is belongs to validate_registry's floor, not here.
    interval = login_tool.get("suggestedRemindAfter")
    check(login_tool.get("blocking") is True and type(interval) is int and interval > 0,
          "login is blocking with a reminder")
    check(manifest.get("login") == {"tool": "login"}, "the manifest names its login tool")
    sys.path.insert(0, str(TREE / "scripts"))
    import sync_lib
    tree = sync_lib.tree
    check(tree(WRAPPER / "_lib") == tree(TREE / "servers" / "_lib"), "the vendored _lib is identical to servers/_lib")


def main() -> int:
    global running
    for fn in (case_cache_store, case_login, case_account_name, case_arguments, case_proxy, case_browser, case_manifest_and_vendoring):
        running = fn.__name__
        print(f"  {running}")
        fn()
    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} self-test assertion(s) failed.")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK: Microsoft 365 wrapper self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
