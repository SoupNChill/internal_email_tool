"""Dashboard authentication.

Two supported arrangements, and refusing to start is the third:

* **Cloudflare Access in front** -- the intended production shape. Set
  `EMAILD_DASHBOARD_BEHIND_PROXY_AUTH=true` to state that plainly.
* **HTTP Basic with a shared token** -- for a LAN deployment with no proxy.
* **Neither, in production** -- rejected at startup (see config.py).

Access must never cover `/v1/*`: a machine client cannot complete an SSO
challenge and would receive an HTML login page where it expects JSON, surfacing
as a parse error rather than an auth error.
"""

from __future__ import annotations

import base64
import hmac

from fastapi import Request
from fastapi.responses import Response

from emaild.config import Settings


def _unauthorised() -> Response:
    return Response(
        status_code=401,
        content="Authentication required.",
        headers={"WWW-Authenticate": 'Basic realm="emaild dashboard"'},
        media_type="text/plain",
    )


def check_dashboard_auth(request: Request, settings: Settings) -> Response | None:
    """Return a 401 response when the request may not view the dashboard.

    Returns None when it may.
    """
    if not settings.dashboard_token:
        # Either development on localhost, or a proxy is authenticating in
        # front. config.py has already refused the unsafe combination.
        return None

    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return _unauthorised()

    try:
        decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - malformed credentials are just a 401
        return _unauthorised()

    _, _, presented = decoded.partition(":")
    # Constant-time: the token is a shared secret, and a length-revealing
    # comparison is free to avoid.
    if not hmac.compare_digest(presented, settings.dashboard_token):
        return _unauthorised()
    return None
