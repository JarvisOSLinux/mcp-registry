#!/usr/bin/env python3
"""Notion MCP wrapper: Notion's hosted server, plus sign-in.

Notion hosts its MCP server at https://mcp.notion.com/mcp and, like every hosted
server, leaves sign-in to the client (the MCP authorization spec). This wrapper
is that client. It adds `login`, `status` and `logout`, runs the browser sign-in
itself (discovery, dynamic client registration, PKCE S256, a one-shot loopback
listener), keeps the token in the keyring or an owner-only file, and relays every
other call to Notion with the token as a bearer.

The token never appears in a tool result or a log line, so it never reaches the
model (Project-JARVIS#242). Every URL, including ones Notion advertises, must be
on mcp.notion.com.

Standard library only, Python 3.10+.
"""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import httpproxy, oauthbrowser, proxy, tokenstore  # noqa: E402
from _lib.devicecode import SignInError  # noqa: E402

SERVICE = "notion"
ENDPOINT = "https://mcp.notion.com/mcp"
ALLOWED_HOSTS = ("mcp.notion.com",)

NOT_SIGNED_IN = "Not signed in to Notion. Call the `login` tool, then retry."

LOCAL_TOOLS = [
    {
        "name": "login",
        "description": (
            "Sign in to Notion. Prints a sign-in link, opens the browser, and waits until the user "
            "approves. Blocks until approved, declined or timed out, so dispatch it as a concurrent "
            "task with a reminder, and tell the user to open the link when it shows up in the output. "
            "The user chooses which pages Notion shares during sign-in. The token is never returned."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "status",
        "description": "Report whether JARVIS is signed in to Notion. Makes no network call.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "logout",
        "description": "Forget the stored Notion sign-in on this machine. Does not revoke it at Notion.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _store() -> tokenstore.TokenStore:
    return tokenstore.TokenStore(SERVICE)


class StoredTokens:
    """The token source for the HTTP proxy: stored, refreshed when it expires."""

    def __init__(self):
        self._lock = threading.Lock()

    def _load(self) -> dict | None:
        try:
            return _store().load()
        except tokenstore.StoreError as err:
            log(f"notion: {err}")
            return None

    def _renew(self, record: dict) -> str | None:
        try:
            fresh = oauthbrowser.refresh(record, ALLOWED_HOSTS)
            _store().save(fresh)
            return fresh["access_token"]
        except (SignInError, tokenstore.StoreError, OSError) as err:
            log(f"notion: could not renew the sign-in: {err}")
            return None

    def token(self) -> str | None:
        with self._lock:
            record = self._load()
            if not record or not record.get("access_token"):
                return None
            if oauthbrowser.expired(record):
                return self._renew(record)
            return str(record["access_token"])

    def refresh_after_401(self) -> str | None:
        with self._lock:
            record = self._load()
            return self._renew(record) if record else None


TOKENS = StoredTokens()


def tool_login(_args: dict) -> str:
    try:
        record = oauthbrowser.login(
            resource=ENDPOINT,
            hosts=ALLOWED_HOSTS,
            log=log,
            open_browser=os.environ.get("JARVIS_SIGNIN_NO_BROWSER") != "1",
        )
    except SignInError as err:
        raise proxy.ToolError(str(err)) from err
    try:
        where = _store().save(record)
    except (tokenstore.StoreError, OSError) as err:
        raise proxy.ToolError(f"signed in, but the token could not be stored: {err}") from err
    log(f"notion: token stored in the {where}")
    return "Signed in to Notion. The token is stored on this machine and is not shown."


def tool_status(_args: dict) -> str:
    try:
        record = _store().load()
    except tokenstore.StoreError as err:
        return f"The stored Notion sign-in cannot be read: {err}"
    if not record or not record.get("access_token"):
        return "Not signed in to Notion. Call the `login` tool."
    if oauthbrowser.expired(record) and not record.get("refresh_token"):
        return "The Notion sign-in has expired. Call the `login` tool."
    return "Signed in to Notion."


def tool_logout(_args: dict) -> str:
    try:
        _store().delete()
    except OSError as err:
        raise proxy.ToolError(f"could not remove the stored sign-in: {err}") from err
    return "Removed the stored Notion sign-in from this machine. To revoke access at Notion, remove the connection in Notion's settings."


HANDLERS = {"login": tool_login, "status": tool_status, "logout": tool_logout}


def main() -> int:
    return httpproxy.run_http_proxy(
        endpoint=ENDPOINT,
        hosts=ALLOWED_HOSTS,
        tokens=TOKENS,
        server_name="notion",
        local_tools=LOCAL_TOOLS,
        handlers=HANDLERS,
        not_signed_in=NOT_SIGNED_IN,
    )


if __name__ == "__main__":
    sys.exit(main())
