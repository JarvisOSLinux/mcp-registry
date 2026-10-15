"""Browser sign-in for a hosted MCP server, by the MCP authorization spec.

A hosted server (Notion's, say) never ships a login tool: the spec gives the
sign-in to the client. A wrapper is that client. This module does it in one
blocking call, for a wrapper's `login` tool:

1. Discovery. The protected resource's metadata (RFC 9728) names the
   authorization server; that server's metadata (RFC 8414) names its endpoints.
2. Registration. Dynamic client registration (RFC 7591) as a public client with
   a loopback redirect, so nobody has to register an app first.
3. Authorization. PKCE (S256), `state`, and the RFC 8707 `resource`. The link is
   printed to stderr (dmcp relays it live and REMIND carries it to the model) and
   opened in the browser through `browser.open_link`.
4. The redirect lands on a one-shot listener on 127.0.0.1. A request without
   this sign-in's `state` is answered and ignored, so a stray or forged
   callback can neither finish nor abort the sign-in.
5. The code is exchanged for tokens. The record returned holds what a refresh
   needs; the caller stores it and never shows it.

Every endpoint goes through `hostpin`: the wrapper names the hosts it trusts.
"""

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Iterable

from . import browser, hostpin

DEFAULT_TIMEOUT_SECONDS = 300
REFRESH_MARGIN_SECONDS = 60

_DONE_PAGE = b"<!doctype html><title>Signed in</title><p>Signed in. You can close this tab and return to JARVIS.</p>"
_FAILED_PAGE = b"<!doctype html><title>Sign-in not completed</title><p>The sign-in was not completed. You can close this tab.</p>"


class SignInError(Exception):
    """The sign-in did not complete. The message is safe to show the model."""


def _split(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")


def _first_json(urls: list, hosts: tuple) -> dict | None:
    for url in urls:
        try:
            value = hostpin.get_json(url, hosts)
        except (hostpin.HostNotAllowed, OSError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def discover(resource: str, allowed_hosts: Iterable[str]) -> dict:
    """The authorization server's endpoints for `resource`, all on pinned hosts."""
    hosts = tuple(allowed_hosts)
    hostpin.check_url(resource, hosts)
    origin, path = _split(resource)
    prm = _first_json(
        [f"{origin}/.well-known/oauth-protected-resource{path}", f"{origin}/.well-known/oauth-protected-resource"],
        hosts,
    )
    servers = (prm or {}).get("authorization_servers") or [origin]
    issuer_origin, issuer_path = _split(str(servers[0]))
    meta = _first_json(
        [
            f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_path}",
            f"{issuer_origin}/.well-known/openid-configuration{issuer_path}",
        ],
        hosts,
    )
    if not meta:
        raise SignInError("the server publishes no sign-in metadata")
    try:
        endpoints = {
            "authorization_endpoint": hostpin.check_url(str(meta.get("authorization_endpoint", "")), hosts),
            "token_endpoint": hostpin.check_url(str(meta.get("token_endpoint", "")), hosts),
        }
        if meta.get("registration_endpoint"):
            endpoints["registration_endpoint"] = hostpin.check_url(str(meta["registration_endpoint"]), hosts)
    except hostpin.HostNotAllowed as err:
        raise SignInError(f"refusing the server's sign-in endpoints: {err}") from err
    methods = meta.get("code_challenge_methods_supported")
    if isinstance(methods, list) and methods and "S256" not in methods:
        raise SignInError("the server does not support PKCE S256")
    scopes = (prm or {}).get("scopes_supported") or meta.get("scopes_supported") or []
    endpoints["scopes"] = [s for s in scopes if isinstance(s, str)]
    return endpoints


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _Callback:
    """The one-shot loopback listener. Ignores requests without our state."""

    def __init__(self, state: str):
        self.state = state
        self.code: str | None = None
        self.error: str | None = None
        self.done = threading.Event()
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                if url.path != "/callback" or query.get("state") != callback.state or callback.done.is_set():
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if query.get("code"):
                    callback.code, page = query["code"], _DONE_PAGE
                else:
                    callback.error, page = query.get("error") or "no code returned", _FAILED_PAGE
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                callback.done.set()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_address[1]}/callback"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _token_record(reply, endpoints: dict, client: dict, resource: str, now: float) -> dict:
    if not isinstance(reply, dict) or not reply.get("access_token"):
        detail = reply.get("error_description") or reply.get("error") if isinstance(reply, dict) else None
        raise SignInError(f"the server did not issue a token: {detail or 'unexpected response'}")
    record = {
        "access_token": reply["access_token"],
        "token_type": reply.get("token_type", "Bearer"),
        "scope": reply.get("scope", ""),
        "token_endpoint": endpoints["token_endpoint"],
        "resource": resource,
        "client_id": client["client_id"],
        "obtained_at": int(now),
    }
    if reply.get("refresh_token"):
        record["refresh_token"] = reply["refresh_token"]
    try:
        expires_in = int(reply.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        record["expires_at"] = int(now) + expires_in
    if client.get("client_secret"):
        record["client_secret"] = client["client_secret"]
    for key in ("workspace_name", "workspace_id", "owner"):
        if key in reply:
            record[key] = reply[key]
    return record


def sign_in(
    *,
    resource: str,
    allowed_hosts: Iterable[str],
    client_name: str,
    log: Callable[[str], None],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    now: Callable[[], float] = time.time,
) -> dict:
    """Run the whole browser sign-in. Returns the token record to store."""
    hosts = tuple(allowed_hosts)
    try:
        endpoints = discover(resource, hosts)
    except (hostpin.HostNotAllowed, OSError, ValueError) as err:
        raise SignInError(f"could not find the server's sign-in: {err}") from err
    if "registration_endpoint" not in endpoints:
        raise SignInError("the server does not allow dynamic client registration")

    callback = _Callback(secrets.token_urlsafe(24))
    try:
        try:
            client = hostpin.post_json(
                endpoints["registration_endpoint"],
                {
                    "client_name": client_name,
                    "redirect_uris": [callback.redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                hosts,
            )
        except (hostpin.HostNotAllowed, OSError, ValueError) as err:
            raise SignInError(f"could not register with the server: {err}") from err
        if not isinstance(client, dict) or not client.get("client_id"):
            raise SignInError("the server refused to register this client")

        verifier, challenge = _pkce()
        query = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": callback.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": callback.state,
            "resource": resource,
        }
        if endpoints["scopes"]:
            query["scope"] = " ".join(endpoints["scopes"])
        link = f"{endpoints['authorization_endpoint']}?{urllib.parse.urlencode(query)}"
        log(f"To sign in, open this link and approve: {link}")
        browser.open_link(link, hosts)

        if not callback.done.wait(timeout):
            raise SignInError("the sign-in was not approved in time")
        if callback.error:
            raise SignInError("sign-in was declined" if callback.error == "access_denied" else f"sign-in failed: {callback.error}")
    finally:
        callback.close()

    fields = {
        "grant_type": "authorization_code",
        "code": callback.code,
        "redirect_uri": callback.redirect_uri,
        "client_id": client["client_id"],
        "code_verifier": verifier,
        "resource": resource,
    }
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    try:
        reply = hostpin.post_form(endpoints["token_endpoint"], fields, hosts)
    except (hostpin.HostNotAllowed, OSError, ValueError) as err:
        raise SignInError(f"could not complete the sign-in: {err}") from err
    return _token_record(reply, endpoints, client, resource, now())


def needs_refresh(record: dict, now: float | None = None) -> bool:
    expires_at = record.get("expires_at")
    return isinstance(expires_at, (int, float)) and (now or time.time()) + REFRESH_MARGIN_SECONDS >= expires_at


def refresh(record: dict, allowed_hosts: Iterable[str], now: Callable[[], float] = time.time) -> dict:
    """A refreshed copy of `record`, bound to the same resource. Raises SignInError."""
    if not record.get("refresh_token"):
        raise SignInError("the sign-in has expired and cannot be renewed; sign in again")
    hosts = tuple(allowed_hosts)
    fields = {
        "grant_type": "refresh_token",
        "refresh_token": record["refresh_token"],
        "client_id": record["client_id"],
        "resource": record["resource"],
    }
    if record.get("client_secret"):
        fields["client_secret"] = record["client_secret"]
    try:
        reply = hostpin.post_form(record["token_endpoint"], fields, hosts)
    except (hostpin.HostNotAllowed, OSError, ValueError) as err:
        raise SignInError(f"could not renew the sign-in: {err}") from err
    client = {"client_id": record["client_id"], "client_secret": record.get("client_secret")}
    fresh = _token_record(reply, {"token_endpoint": record["token_endpoint"]}, client, record["resource"], now())
    # Servers that do not rotate refresh tokens omit it on refresh.
    fresh.setdefault("refresh_token", record["refresh_token"])
    for key in ("workspace_name", "workspace_id", "owner"):
        if key in record and key not in fresh:
            fresh[key] = record[key]
    return fresh
