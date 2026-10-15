#!/usr/bin/env python3
"""Notion MCP wrapper: Notion's hosted server, with the sign-in a client owes it.

Notion's official MCP server runs at https://mcp.notion.com/mcp and signs its
caller in under the MCP authorization spec. That flow belongs to the client, so
Notion will never ship a `login` tool (Project-JARVIS#229, design v3). This
wrapper is the client: a local stdio server that

- runs the browser sign-in in `login` (`_lib.oauthbrowser`): discovery, dynamic
  registration, PKCE, the link to stderr and the browser, the redirect on a
  one-shot 127.0.0.1 listener. The user only approves the pages to share;
- keeps the tokens in the keyring or an owner-only file (`_lib.tokenstore`),
  renews them before they expire or after a 401, and never returns them;
- forwards every other request to Notion over HTTPS (`_lib.httpproxy`).

Only mcp.notion.com is trusted: discovery, registration, the token exchange and
every forwarded call are pinned to it.

Standard library only, Python 3.10+.
"""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import httpproxy, oauthbrowser, tokenstore  # noqa: E402

SERVICE = "notion"
RESOURCE = "https://mcp.notion.com/mcp"
ALLOWED_HOSTS = ("mcp.notion.com",)
CLIENT_NAME = "JARVIS"
SERVER_INFO = {"name": "notion (JARVIS wrapper)", "version": "1.0.0"}

NOT_SIGNED_IN = "Not signed in to Notion. Call the `login` tool, then retry."

# Notion's live server exposes more than its documentation lists. These two move
# many pages, or a database with all its rows, to Trash in one call: the mass
# deletion the forbidden tier covers (docs/manifest-reference.md, #88), even
# though Trash can restore them. Withheld until the daemon enforces that tier.
HIDDEN_TOOLS = frozenset({"notion-delete-pages", "notion-delete-databases"})

LOCAL_TOOLS = [
    {
        "name": "login",
        "description": (
            "Sign in to Notion. Opens Notion's sign-in page in the browser (the link is also printed) "
            "and waits until the user approves which pages and databases to share. Blocks until "
            "approved, declined or timed out, so dispatch it as a concurrent task with a reminder, "
            "and tell the user about the link when it shows up in the output. Returns which workspace "
            "was connected; tokens are never returned."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "status",
        "description": "Report whether JARVIS is signed in to Notion, and to which workspace. Makes no network call.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "logout",
        "description": "Forget the stored Notion sign-in on this machine. To remove access entirely, disconnect JARVIS in Notion's settings.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

_renew_lock = threading.Lock()


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _store() -> tokenstore.TokenStore:
    return tokenstore.TokenStore(SERVICE)


def _record() -> dict | None:
    try:
        record = _store().load()
    except tokenstore.StoreError as err:
        log(f"notion: {err}")
        return None
    return record if isinstance(record, dict) and record.get("access_token") else None


def _workspace(record: dict) -> str:
    name = str(record.get("workspace_name") or "").strip()
    return f" (workspace {name})" if name else ""


def get_token(renew: bool) -> str | None:
    """The access token, renewed first when it is about to expire or after a 401."""
    with _renew_lock:
        record = _record()
        if not record:
            return None
        if renew or oauthbrowser.needs_refresh(record):
            try:
                record = oauthbrowser.refresh(record, ALLOWED_HOSTS)
            except oauthbrowser.SignInError as err:
                log(f"notion: {err}")
                return None
            _store().save(record)
        return record["access_token"]


def tool_login(_args: dict) -> str:
    try:
        record = oauthbrowser.sign_in(
            resource=RESOURCE,
            allowed_hosts=ALLOWED_HOSTS,
            client_name=CLIENT_NAME,
            log=log,
            timeout=float(os.environ.get("JARVIS_SIGNIN_TIMEOUT") or oauthbrowser.DEFAULT_TIMEOUT_SECONDS),
        )
    except oauthbrowser.SignInError as err:
        raise httpproxy.ToolError(str(err)) from err
    try:
        where = _store().save(record)
    except (tokenstore.StoreError, OSError) as err:
        raise httpproxy.ToolError(f"signed in, but the sign-in could not be stored: {err}") from err
    log(f"notion: sign-in stored in the {where}")
    return f"Signed in to Notion{_workspace(record)}. The tokens are kept on this machine and are not shown."


def tool_status(_args: dict) -> str:
    record = _record()
    if not record:
        return "Not signed in to Notion. Call the `login` tool."
    return f"Signed in to Notion{_workspace(record)}."


def tool_logout(_args: dict) -> str:
    try:
        _store().delete()
    except OSError as err:
        raise httpproxy.ToolError(f"could not remove the stored sign-in: {err}") from err
    return (
        "Removed the stored Notion sign-in from this machine. To revoke JARVIS's access in Notion too, "
        "disconnect it under Settings > Connections."
    )


HANDLERS = {"login": tool_login, "status": tool_status, "logout": tool_logout}


def main() -> int:
    return httpproxy.run_http_proxy(
        url=RESOURCE,
        allowed_hosts=ALLOWED_HOSTS,
        get_token=get_token,
        local_tools=LOCAL_TOOLS,
        handlers=HANDLERS,
        server_info=SERVER_INFO,
        not_signed_in=NOT_SIGNED_IN,
        hidden_tools=HIDDEN_TOOLS,
    )


if __name__ == "__main__":
    sys.exit(main())
