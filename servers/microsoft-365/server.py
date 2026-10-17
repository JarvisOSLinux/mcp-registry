#!/usr/bin/env python3
"""Microsoft 365 MCP wrapper: Softeria's server, with a sign-in that completes.

The upstream server (@softeria/ms-365-mcp-server, installed into ./upstream by
setup.sh at a pinned version) already signs in by itself, so this wrapper reuses
its sign-in rather than writing one (Project-JARVIS#229, design v3). It changes
three things, and forwards everything else unchanged:

- `login`: the upstream's own `login` tool returns the device code at once and
  finishes the sign-in in the background of the same process. dmcp starts a
  fresh server process per tool call and ends it after the answer, so that
  background sign-in never completes. This `login` runs the upstream's blocking
  `--login` command instead: it prints the link and code to stderr (dmcp relays
  them live, and REMIND carries them to the model), opens the browser, and
  returns who signed in once the user approves.
- Token storage: MS365_MCP_AUTH_CACHE_COMMAND points the upstream at
  cache_store.py, so its cache lives in the OS keyring (or an owner-only file),
  not in a file whose key sits beside it.
- Withheld tools: tools in the forbidden classes of the threat model (mass
  deletion, an arbitrary Graph passthrough) and webhook subscriptions are hidden
  and refused. See HIDDEN_TOOLS.

The upstream's `verify-login`, `logout` and account tools work as they are: they
read the same cache.

Standard library only, Python 3.10+.
"""

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import browser, hostpin, proxy, reviewed  # noqa: E402

# Only tools the registry reviewed (this wrapper's manifest) are passed on.
REVIEWED_TOOLS = reviewed.declared_tools(__file__)

HERE = Path(__file__).resolve().parent
UPSTREAM_DIR = HERE / "upstream"
UPSTREAM_ENTRY = UPSTREAM_DIR / "node_modules" / "@softeria" / "ms-365-mcp-server" / "dist" / "index.js"
CACHE_COMMAND = HERE / "cache_store.py"
NODE = "node"

# Where Microsoft's device sign-in sends people. A link on any other host is
# refused before it is shown or opened.
SIGN_IN_HOSTS = ("microsoft.com", "www.microsoft.com", "login.microsoft.com", "login.microsoftonline.com")

DEFAULT_PRESET = "mail,calendar"
_PRESET_RE = re.compile(r"^[a-z][a-z-]*(,[a-z][a-z-]*)*$")

# Device codes live 15 minutes; past this the sign-in has certainly failed.
LOGIN_TIMEOUT_SECONDS = 16 * 60
VERIFY_TIMEOUT_SECONDS = 60

HIDDEN_TOOLS = frozenset(
    {
        # Up to 20 arbitrary Graph requests per call: the confirmation gate cannot
        # see what is inside, so it is a way around every other classification.
        "graph-batch",
        # Each deletes a container and everything in it: mass deletion, which the
        # forbidden tier covers (docs/manifest-reference.md, #88).
        "delete-calendar",
        "delete-mail-folder",
        "delete-contact-folder",
        "delete-planner-bucket",
        "delete-todo-task-list",
        # Webhooks push change notifications about the mailbox and files to a URL
        # of the caller's choosing: a standing channel out that JARVIS never needs.
        "create-subscription",
        "update-subscription",
        "reauthorize-subscription",
    }
)

LOGIN_TOOL = {
    "name": "login",
    "description": (
        "Sign in to Microsoft 365 (Outlook mail, calendar, OneDrive and more). Starts Microsoft's "
        "device sign-in: prints a link and a short code, opens the browser, and waits until the "
        "user approves. Blocks until approved, declined or expired, so dispatch it as a "
        "concurrent task with a reminder, and tell the user the link and code when they show up "
        "in the output. Does nothing if already signed in, unless force is true. Returns who "
        "signed in; tokens are never returned."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "force": {"type": "boolean", "description": "Sign in again even if already signed in."}
        },
        "additionalProperties": False,
    },
}

_LINK_RE = re.compile(r"https://[^\s\"'<>]+")
_CODE_RE = re.compile(r"\bcode\s+([A-Z0-9]{6,12})\b")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def upstream_args() -> list[str]:
    """Arguments shared by the server and its login, so the token is requested
    for exactly the tools the server will expose."""
    preset = (os.environ.get("MS365_PRESET") or DEFAULT_PRESET).strip().lower()
    if not _PRESET_RE.match(preset):
        raise proxy.ToolError(f"MS365_PRESET {preset!r} is not a comma-separated list of preset names")
    args = [] if preset == "all" else ["--preset", preset]
    if _truthy(os.environ.get("MS365_READ_ONLY")):
        args.append("--read-only")
    return args


def upstream_env() -> dict:
    env = dict(os.environ)
    env.setdefault("MS365_MCP_AUTH_CACHE_COMMAND", str(CACHE_COMMAND))
    return env


def _account(result: dict) -> str:
    user = result.get("userData") if isinstance(result.get("userData"), dict) else {}
    # Personal accounts often carry a blank display name.
    name = str(user.get("displayName") or "").strip()
    upn = str(user.get("userPrincipalName") or "").strip()
    if name and upn:
        return f" as {name} ({upn})"
    return f" as {name or upn}" if (name or upn) else ""


def _last_json(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _verify() -> dict | None:
    try:
        done = subprocess.run(
            [NODE, str(UPSTREAM_ENTRY), *upstream_args(), "--verify-login"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=upstream_env(),
            cwd=str(UPSTREAM_DIR),
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _last_json(done.stdout)


def _relay(stream, sink: list) -> None:
    for line in stream:
        sink.append(line)
        if line.strip():
            log(f"microsoft-365: {line.rstrip()}")


def tool_login(args: dict) -> str:
    if not args.get("force"):
        status = _verify()
        if status and status.get("success"):
            return f"Already signed in to Microsoft 365{_account(status)}. Pass force to sign in again."

    child = subprocess.Popen(
        [NODE, str(UPSTREAM_ENTRY), *upstream_args(), "--login"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=upstream_env(),
        cwd=str(UPSTREAM_DIR),
    )
    errors: list = []
    threading.Thread(target=_relay, args=(child.stderr, errors), daemon=True).start()
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        child.kill()

    timer = threading.Timer(LOGIN_TIMEOUT_SECONDS, expire)
    timer.start()
    out: list = []
    shown = False
    try:
        assert child.stdout is not None
        for line in child.stdout:
            out.append(line)
            link = _LINK_RE.search(line)
            if shown or not link:
                continue
            url = link.group(0).rstrip(".,;)")
            try:
                hostpin.check_url(url, SIGN_IN_HOSTS)
            except hostpin.HostNotAllowed as err:
                child.kill()
                raise proxy.ToolError(f"refusing the sign-in link: {err}") from err
            code = _CODE_RE.search(line)
            log(f"To sign in to Microsoft 365, open {url} and enter the code {code.group(1) if code else '(see above)'}")
            browser.open_link(url, SIGN_IN_HOSTS)
            if code:
                browser.notify("Microsoft 365 sign-in", f"Enter the code {code.group(1)} at {url}")
            shown = True
        child.wait()
    finally:
        timer.cancel()

    result = _last_json("".join(out))
    if result and result.get("success"):
        return (
            f"Signed in to Microsoft 365{_account(result)}. The tokens are kept on this machine "
            "and are not shown."
        )
    if expired.is_set():
        raise proxy.ToolError("the sign-in code expired before it was approved")
    if not shown:
        raise proxy.ToolError("sign-in did not start: " + ("".join(errors).strip()[-300:] or "no output"))
    if result:
        raise proxy.ToolError(f"sign-in failed: {result.get('message') or 'unknown reason'}")
    raise proxy.ToolError("the sign-in did not complete (declined, or the code expired)")


def main() -> int:
    if not UPSTREAM_ENTRY.is_file():
        log(f"microsoft-365: upstream server not installed ({UPSTREAM_ENTRY}); run this server's setup again")
        return 1
    try:
        argv = [NODE, str(UPSTREAM_ENTRY), *upstream_args()]
    except proxy.ToolError as err:
        log(f"microsoft-365: {err}")
        return 1
    return proxy.run_proxy(
        upstream_argv=argv,
        upstream_env=upstream_env(),
        upstream_cwd=str(UPSTREAM_DIR),
        local_tools=[LOGIN_TOOL],
        handlers={"login": tool_login},
        hidden_tools=HIDDEN_TOOLS,
        allowed_tools=REVIEWED_TOOLS,
    )


if __name__ == "__main__":
    sys.exit(main())
