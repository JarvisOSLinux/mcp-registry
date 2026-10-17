"""Open a sign-in link in the user's browser without touching the MCP wire.

A stdio MCP server's stdin and stdout are the JSON-RPC channel. `webbrowser.open`
starts the browser with both inherited: a browser that prints to stdout ("Opening
in existing browser session.") lands mid-stream in the protocol, and with no
display Python falls back to a console browser (w3m, lynx) that runs in the
foreground on the protocol pipes and blocks the sign-in. So the opener runs
detached, on /dev/null, and only where there is a desktop to open on. The link is
printed to stderr either way, so a missing opener only costs a click.
"""

import os
import shutil
import subprocess
import sys
from typing import Iterable

from . import hostpin


def _opener() -> list[str] | None:
    if sys.platform == "darwin":
        return ["open"]
    if os.name == "nt":
        # Not `cmd /c start`: cmd would read a URL's `&` as a command separator.
        return ["rundll32", "url.dll,FileProtocolHandler"]
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return ["xdg-open"]
    return None


def open_link(url: str, allowed_hosts: Iterable[str]) -> bool:
    """Open `url` in the browser if it is on a pinned host. True if an opener started.

    Raises hostpin.HostNotAllowed for a link outside `allowed_hosts`.
    JARVIS_SIGNIN_NO_BROWSER=1 turns opening off.
    """
    hostpin.check_url(url, allowed_hosts)
    if os.environ.get("JARVIS_SIGNIN_NO_BROWSER") == "1":
        return False
    command = _opener()
    if not command:
        return False
    try:
        subprocess.Popen(
            [*command, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except OSError:
        return False
    return True


def _notifier(title: str, body: str) -> list[str] | None:
    if sys.platform == "darwin":
        quote = lambda text: '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return ["osascript", "-e", f"display notification {quote(body)} with title {quote(title)}"]
    if os.name == "nt":
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    notify_send = shutil.which("notify-send")
    return [notify_send, "--app-name=JARVIS", "--expire-time=0", title, body] if notify_send else None


def notify(title: str, body: str) -> bool:
    """Show a desktop notification, detached and off the MCP wire. True if one started.

    A device-code sign-in prints its code to stderr, which reaches the model
    only at the call's first reminder, minutes later. A notification puts the
    code in front of the user the moment it exists, even while the model is
    busy. JARVIS_SIGNIN_NO_BROWSER=1 turns this off with the browser.
    """
    if os.environ.get("JARVIS_SIGNIN_NO_BROWSER") == "1":
        return False
    command = _notifier(title, body)
    if not command:
        return False
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except OSError:
        return False
    return True
