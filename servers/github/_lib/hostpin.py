"""Host pinning: a wrapper only talks to, or opens links on, the hosts it names.

The sign-in link or device code reaches the model and the user's browser, so the
host check is what keeps "vetting the wrapper keeps the links honest" true.
Endpoints advertised by a remote party (a verification URI, a redirect) go
through the same check as the wrapper's own constants.
"""

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

MAX_RESPONSE_BYTES = 1 << 20
TIMEOUT_SECONDS = 20
STREAM_TIMEOUT_SECONDS = 600
USER_AGENT = "jarvis-signin/1.0 (+https://github.com/JarvisOSLinux/mcp-registry)"


class HostNotAllowed(Exception):
    """A URL fell outside the hosts the wrapper pinned."""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """Return `url` if it is https (or loopback http) on a pinned host."""
    if not isinstance(url, str):
        raise HostNotAllowed("link is not a string")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password:
        raise HostNotAllowed("link carries credentials")
    if parts.scheme != "https" and not (parts.scheme == "http" and _is_loopback(host)):
        raise HostNotAllowed(f"link scheme {parts.scheme!r} is not https")
    if host not in {h.lower() for h in allowed_hosts}:
        raise HostNotAllowed(f"host {host!r} is not one this wrapper names")
    return url


class PinnedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Iterable[str]):
        self._allowed = tuple(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self._allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(req: urllib.request.Request, allowed_hosts: Iterable[str]) -> Any:
    check_url(req.full_url, allowed_hosts)
    opener = urllib.request.build_opener(PinnedRedirects(allowed_hosts))
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as err:
        # OAuth servers answer 400 with a JSON error body ("authorization_pending").
        body = err.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError("response is not JSON") from err


def post_form(url: str, fields: dict, allowed_hosts: Iterable[str]) -> Any:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    return _request(req, allowed_hosts)


def get_json(url: str, allowed_hosts: Iterable[str], headers: dict | None = None) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
    )
    return _request(req, allowed_hosts)


def post_json(url: str, obj: Any, allowed_hosts: Iterable[str], headers: dict | None = None) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(obj).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
    )
    return _request(req, allowed_hosts)


def open_stream(req: urllib.request.Request, allowed_hosts: Iterable[str]):
    """Open a request whose body may stream (server-sent events).

    Returns (status, headers, file-like). An HTTP error status is returned, not
    raised, so the caller can read a 401's headers.
    """
    check_url(req.full_url, allowed_hosts)
    opener = urllib.request.build_opener(PinnedRedirects(allowed_hosts))
    try:
        resp = opener.open(req, timeout=STREAM_TIMEOUT_SECONDS)
        return resp.status, resp.headers, resp
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err
