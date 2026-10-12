"""Browser sign-in for a hosted MCP server, under the MCP authorization spec.

The wrapper is the OAuth client: it discovers the authorization server
(RFC 9728 then RFC 8414), registers itself (RFC 7591), and signs the user in with
the authorization code flow and PKCE S256, sending `resource` (RFC 8707). The
browser comes back to a one-shot listener on 127.0.0.1 that ignores any request
without this sign-in's `state`, so a forged or stray callback can neither finish
nor abort it. Every endpoint, including the ones the server advertises, is host
pinned. Tokens are returned to the caller, never logged.
"""

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Callable, Iterable

from . import hostpin
from .devicecode import SignInError

SKEW_SECONDS = 60
LOGIN_TIMEOUT_SECONDS = 300
CLIENT_NAME = "JARVIS"


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _fetch_first(candidates: list[str], hosts, need: tuple) -> dict | None:
    for url in candidates:
        try:
            doc = hostpin.get_json(url, hosts)
        except (hostpin.HostNotAllowed, OSError, ValueError):
            continue
        if isinstance(doc, dict) and all(doc.get(key) for key in need):
            return doc
    return None


def discover(resource: str, hosts: Iterable[str]) -> dict:
    """Find the authorization server for `resource`. Returns its metadata plus `resource`."""
    hosts = tuple(hosts)
    path = urllib.parse.urlsplit(resource).path.rstrip("/")
    base = _origin(resource)
    prm = _fetch_first(
        [f"{base}/.well-known/oauth-protected-resource{path}", f"{base}/.well-known/oauth-protected-resource"],
        hosts,
        ("authorization_servers",),
    )
    issuer = str(prm["authorization_servers"][0]) if prm else base
    ipath = urllib.parse.urlsplit(issuer).path.rstrip("/")
    ibase = _origin(issuer)
    meta = _fetch_first(
        [f"{ibase}/.well-known/oauth-authorization-server{ipath}", f"{ibase}/.well-known/openid-configuration{ipath}"],
        hosts,
        ("authorization_endpoint", "token_endpoint"),
    )
    if not meta:
        raise SignInError("the server does not advertise how to sign in")
    methods = meta.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise SignInError("the server does not support PKCE S256")
    if not meta.get("registration_endpoint"):
        raise SignInError("the server has no client registration endpoint")
    meta["resource"] = str(prm.get("resource")) if prm and prm.get("resource") else resource
    return meta


def _register(meta: dict, redirect_uri: str, hosts) -> dict:
    try:
        reply = hostpin.post_json(
            meta["registration_endpoint"],
            {
                "client_name": CLIENT_NAME,
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            hosts,
        )
    except (hostpin.HostNotAllowed, OSError, ValueError) as err:
        raise SignInError(f"could not register with the server: {err}") from err
    if not isinstance(reply, dict) or not reply.get("client_id"):
        raise SignInError("the server refused client registration")
    return reply


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Listener(http.server.HTTPServer):
    expected_state = ""
    result: dict | None = None


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        if parts.path != "/callback" or query.get("state") != self.server.expected_state:
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = query
        body = b"<!doctype html><title>Signed in</title><p>You can close this tab and return to JARVIS.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _record(reply: dict, meta: dict, client: dict, resource: str, previous: dict | None = None) -> dict:
    expires_in = reply.get("expires_in")
    return {
        "access_token": reply["access_token"],
        "refresh_token": reply.get("refresh_token") or (previous or {}).get("refresh_token", ""),
        "expires_at": int(time.time() + int(expires_in)) if expires_in else 0,
        "token_type": reply.get("token_type", "Bearer"),
        "scope": reply.get("scope", ""),
        "token_endpoint": meta["token_endpoint"],
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "resource": resource,
    }


def _token_request(token_endpoint: str, fields: dict, hosts) -> dict:
    try:
        reply = hostpin.post_form(token_endpoint, fields, hosts)
    except (hostpin.HostNotAllowed, OSError, ValueError) as err:
        raise SignInError(f"lost contact with the server: {err}") from err
    if not isinstance(reply, dict) or not reply.get("access_token"):
        detail = reply.get("error_description") or reply.get("error") if isinstance(reply, dict) else None
        raise SignInError(f"the server refused the token request: {detail or 'unexpected response'}")
    return reply


def login(
    *,
    resource: str,
    hosts: Iterable[str],
    log: Callable[[str], None],
    open_browser: bool = True,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> dict:
    """Run the browser sign-in. Returns a token record for `refresh`/`TokenSource`."""
    hosts = tuple(hosts)
    meta = discover(resource, hosts)
    resource = meta["resource"]

    listener = _Listener(("127.0.0.1", 0), _Handler)
    try:
        redirect_uri = f"http://127.0.0.1:{listener.server_port}/callback"
        client = _register(meta, redirect_uri, hosts)
        verifier, challenge = _pkce()
        listener.expected_state = secrets.token_urlsafe(24)

        query = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": listener.expected_state,
            "resource": resource,
        }
        if meta.get("scopes_supported"):
            query["scope"] = " ".join(meta["scopes_supported"])
        try:
            url = hostpin.check_url(
                f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(query)}", hosts
            )
        except hostpin.HostNotAllowed as err:
            raise SignInError(f"refusing the server's sign-in link: {err}") from err

        log(f"To sign in, open this link: {url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        listener.timeout = 1
        deadline = time.monotonic() + timeout
        while listener.result is None and time.monotonic() < deadline:
            listener.handle_request()
    finally:
        listener.server_close()

    result = listener.result
    if result is None:
        raise SignInError("sign-in was not completed in time")
    if result.get("error"):
        raise SignInError("sign-in was declined" if result["error"] == "access_denied" else f"sign-in failed: {result['error']}")
    if not result.get("code"):
        raise SignInError("the server returned no authorization code")

    fields = {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": client["client_id"],
        "code_verifier": verifier,
        "resource": resource,
    }
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    reply = _token_request(meta["token_endpoint"], fields, hosts)
    return _record(reply, meta, client, resource)


def expired(record: dict, now: Callable[[], float] = time.time) -> bool:
    at = int(record.get("expires_at") or 0)
    return bool(at) and now() >= at - SKEW_SECONDS


def refresh(record: dict, hosts: Iterable[str]) -> dict:
    """Exchange the refresh token. Raises SignInError if there is none or it fails."""
    if not record.get("refresh_token"):
        raise SignInError("the sign-in expired and cannot be renewed; sign in again")
    fields = {
        "grant_type": "refresh_token",
        "refresh_token": record["refresh_token"],
        "client_id": record["client_id"],
        "resource": record["resource"],
    }
    if record.get("client_secret"):
        fields["client_secret"] = record["client_secret"]
    hosts = tuple(hosts)
    reply = _token_request(record["token_endpoint"], fields, hosts)
    return _record(
        reply,
        {"token_endpoint": record["token_endpoint"]},
        {"client_id": record["client_id"], "client_secret": record.get("client_secret", "")},
        record["resource"],
        previous=record,
    )
