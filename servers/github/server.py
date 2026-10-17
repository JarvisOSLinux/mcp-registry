#!/usr/bin/env python3
"""GitHub MCP wrapper: the upstream GitHub server, plus sign-in.

The upstream server (MissionSquad/mcp-github, built into ./upstream by
setup.sh) takes a personal access token and has no login of its own. This
wrapper adds three tools -- `login`, `status` and `logout` -- and starts the
upstream with the stored token, so the user never creates or pastes a token.

`login` runs GitHub's OAuth device flow in one blocking call. It prints the
verification link and code to stderr (dmcp relays that live), opens the browser,
and waits. The token is stored in the keyring or an owner-only file and is never
returned in a tool result, so it never reaches the model (Project-JARVIS#242).

A token set by hand in GITHUB_PERSONAL_ACCESS_TOKEN still wins over a stored one.

Standard library only, Python 3.10+.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import devicecode, hostpin, proxy, reviewed, tokenstore  # noqa: E402

# Only tools the registry reviewed (this wrapper's manifest) are passed on.
REVIEWED_TOOLS = reviewed.declared_tools(__file__)

SERVICE = "github"
ALLOWED_HOSTS = ("github.com", "api.github.com")
DEVICE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
IDENTITY_URL = "https://api.github.com/user"

# The public client id of the JarvisOS GitHub OAuth App (Device Flow enabled).
# Public by design: a device-flow app has no secret. GITHUB_OAUTH_CLIENT_ID
# overrides it, so a personal OAuth app can test the flow end to end.
DEFAULT_CLIENT_ID = "Ov23liURTWQGuxqD0yFv"
DEFAULT_SCOPES = "repo"

UPSTREAM_DIR = Path(__file__).resolve().parent / "upstream"
UPSTREAM_ENTRY = UPSTREAM_DIR / "dist" / "index.js"
TOKEN_ENV = "GITHUB_PERSONAL_ACCESS_TOKEN"

NOT_SIGNED_IN = "Not signed in to GitHub. Call the `login` tool, then retry."

LOCAL_TOOLS = [
    {
        "name": "login",
        "description": (
            "Sign in to GitHub. Starts GitHub's device sign-in: prints a link and a short code, "
            "opens the browser, and waits until the user approves. Blocks until approved, declined "
            "or expired, so dispatch it as a concurrent task with a reminder. Tell the user to open "
            "the link and enter the code when it shows up in the output. Returns who signed in; the "
            "token itself is never returned."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "status",
        "description": "Report whether JARVIS is signed in to GitHub, and as whom. Makes no network call.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "logout",
        "description": "Forget the stored GitHub sign-in on this machine. Does not revoke the token at GitHub.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _store() -> tokenstore.TokenStore:
    return tokenstore.TokenStore(SERVICE)


def _client_id() -> str:
    return os.environ.get("GITHUB_OAUTH_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID


def _hand_set_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def _stored_record() -> dict | None:
    try:
        return _store().load()
    except tokenstore.StoreError as err:
        log(f"github: {err}")
        return None


def current_token() -> str:
    """The token the upstream should run with: hand-set first, then stored."""
    hand = _hand_set_token()
    if hand:
        return hand
    record = _stored_record()
    return str(record.get("access_token", "")) if record else ""


def tool_login(_args: dict) -> str:
    client_id = _client_id()
    if not client_id:
        raise proxy.ToolError(
            "GitHub sign-in is not configured: no OAuth App client id. Set GITHUB_OAUTH_CLIENT_ID "
            "for this server, or use a personal access token instead."
        )
    scopes = os.environ.get("GITHUB_OAUTH_SCOPES", "").split() or DEFAULT_SCOPES.split()
    try:
        grant = devicecode.run_device_flow(
            client_id=client_id,
            scopes=scopes,
            device_url=DEVICE_URL,
            token_url=TOKEN_URL,
            allowed_hosts=ALLOWED_HOSTS,
            log=log,
            open_browser=os.environ.get("JARVIS_SIGNIN_NO_BROWSER") != "1",
        )
    except devicecode.SignInError as err:
        raise proxy.ToolError(str(err)) from err

    token = grant["access_token"]
    login = ""
    try:
        who = hostpin.get_json(
            IDENTITY_URL, ALLOWED_HOSTS, {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
        )
        login = str(who.get("login", "")) if isinstance(who, dict) else ""
    except (hostpin.HostNotAllowed, OSError, ValueError):
        pass

    record = {
        "access_token": token,
        "token_type": grant.get("token_type", "bearer"),
        "scope": grant.get("scope", ""),
        "login": login,
        "obtained_at": int(time.time()),
    }
    try:
        where = _store().save(record)
    except (tokenstore.StoreError, OSError) as err:
        raise proxy.ToolError(f"signed in, but the token could not be stored: {err}") from err
    log(f"github: token stored in the {where}")
    who_text = f" as {login}" if login else ""
    return f"Signed in to GitHub{who_text}. The token is stored on this machine and is not shown."


def tool_status(_args: dict) -> str:
    if _hand_set_token():
        return "Using a personal access token set by hand in this server's config."
    record = _stored_record()
    if not record or not record.get("access_token"):
        return "Not signed in to GitHub. Call the `login` tool."
    who = record.get("login") or "an unknown account"
    scope = record.get("scope") or "unknown scopes"
    return f"Signed in to GitHub as {who} (scopes: {scope})."


def tool_logout(_args: dict) -> str:
    try:
        _store().delete()
    except OSError as err:
        raise proxy.ToolError(f"could not remove the stored sign-in: {err}") from err
    return (
        "Removed the stored GitHub sign-in from this machine. To revoke the token at GitHub, "
        "remove the app at https://github.com/settings/applications."
    )


HANDLERS = {"login": tool_login, "status": tool_status, "logout": tool_logout}


def guard(_name: str, _args: dict) -> str | None:
    return None if current_token() else NOT_SIGNED_IN


def main() -> int:
    if not UPSTREAM_ENTRY.is_file():
        log(f"github: upstream server not built ({UPSTREAM_ENTRY}); run this server's setup again")
        return 1
    env = dict(os.environ)
    token = current_token()
    if token:
        env[TOKEN_ENV] = token
    if sys.argv[1:] == [reviewed.LIST_FLAG]:
        return reviewed.print_upstream_tools(
            lambda: proxy.list_upstream_tools(
                upstream_argv=["node", str(UPSTREAM_ENTRY)], upstream_env=env, upstream_cwd=str(UPSTREAM_DIR)
            ),
            LOCAL_TOOLS,
        )
    return proxy.run_proxy(
        upstream_argv=["node", str(UPSTREAM_ENTRY)],
        upstream_env=env,
        upstream_cwd=str(UPSTREAM_DIR),
        local_tools=LOCAL_TOOLS,
        handlers=HANDLERS,
        guard=guard,
        allowed_tools=REVIEWED_TOOLS,
    )


if __name__ == "__main__":
    sys.exit(main())
