"""OAuth 2.0 device authorization grant (RFC 8628), for a wrapper's login tool.

One blocking call: it prints the verification link and user code to stderr (dmcp
relays stderr live and REMIND carries it to the model), opens the browser, and
polls until the user approves, declines or the code expires. The token is only
ever returned to the caller, never logged.
"""

import time
import webbrowser
from typing import Callable, Iterable

from . import hostpin

DEFAULT_INTERVAL = 5
DEFAULT_EXPIRES_IN = 900
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class SignInError(Exception):
    """The sign-in did not complete. The message is safe to show the model."""


def run_device_flow(
    *,
    client_id: str,
    scopes: Iterable[str],
    device_url: str,
    token_url: str,
    allowed_hosts: Iterable[str],
    log: Callable[[str], None],
    open_browser: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    hosts = tuple(allowed_hosts)
    try:
        grant = hostpin.post_form(
            device_url, {"client_id": client_id, "scope": " ".join(scopes)}, hosts
        )
    except (hostpin.HostNotAllowed, OSError, ValueError) as err:
        raise SignInError(f"could not start sign-in: {err}") from err

    if not isinstance(grant, dict) or "device_code" not in grant:
        detail = grant.get("error_description") or grant.get("error") if isinstance(grant, dict) else None
        raise SignInError(f"provider refused to start sign-in: {detail or 'unexpected response'}")

    device_code = grant["device_code"]
    user_code = str(grant.get("user_code", ""))
    try:
        link = hostpin.check_url(str(grant.get("verification_uri", "")), hosts)
        complete = grant.get("verification_uri_complete")
        browser_link = hostpin.check_url(complete, hosts) if complete else link
    except hostpin.HostNotAllowed as err:
        raise SignInError(f"refusing the provider's sign-in link: {err}") from err

    log(f"To sign in, open {link} and enter the code {user_code}")
    if open_browser:
        try:
            webbrowser.open(browser_link)
        except Exception:
            pass

    interval = max(int(grant.get("interval") or DEFAULT_INTERVAL), 1)
    deadline = now() + int(grant.get("expires_in") or DEFAULT_EXPIRES_IN)

    while now() < deadline:
        sleep(interval)
        try:
            reply = hostpin.post_form(
                token_url,
                {"client_id": client_id, "device_code": device_code, "grant_type": GRANT_TYPE},
                hosts,
            )
        except (hostpin.HostNotAllowed, OSError, ValueError) as err:
            raise SignInError(f"lost contact with the provider: {err}") from err
        if not isinstance(reply, dict):
            raise SignInError("unexpected reply from the provider")
        if reply.get("access_token"):
            return reply
        error = reply.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = max(int(reply.get("interval") or 0), interval + 5)
            continue
        if error == "expired_token":
            raise SignInError("the sign-in code expired before it was approved")
        if error == "access_denied":
            raise SignInError("sign-in was declined")
        raise SignInError(f"sign-in failed: {reply.get('error_description') or error or 'unknown error'}")

    raise SignInError("the sign-in code expired before it was approved")
