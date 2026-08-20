"""Cross-site request forgery protection for dashboard mutations.

Necessary specifically because the dashboard authenticates with HTTP Basic.
Browsers attach Basic credentials to **every** request to an origin they have
credentials for, including a form submitted by a page on some other site. So
the moment the dashboard grew a POST route, any page the operator happened to
visit could have created an API key on their emaild -- with no XSS, no stolen
password, and nothing in the logs that looks unusual.

Two independent checks, because each fails differently:

1. **A synchronizer token** in every form. Deriving it from the dashboard token
   means only someone who can already authenticate can compute it, and the
   same-origin policy stops an attacker's page from reading one out of a
   rendered form.

2. **An Origin/Referer check.** Cheap, and catches the case where a token has
   leaked -- into a screenshot, a shared terminal, a bug report.

Neither is sufficient alone. A static token is forgeable once disclosed; an
Origin header is absent on some legitimate clients. Requiring the token always
and the Origin only when present is the combination that holds.

NOT a general session system. There is one shared credential and one operator;
anything more would be inventing a user model this tool does not have.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from urllib.parse import urlsplit

from fastapi import Request

from emaild.config import Settings

_FIELD = "csrf_token"

# Used only when no dashboard token exists -- development on localhost, or a
# proxy authenticating in front. Regenerated per process, so a restart
# invalidates any form still open. That is correct: it is a security boundary,
# not a convenience, and the API runs as a single uvicorn process so every
# request shares this value.
_PROCESS_SECRET = secrets.token_urlsafe(32)

_DERIVATION_LABEL = b"emaild-dashboard-csrf-v1"


def issue_token(settings: Settings) -> str:
    """The value to embed in a form.

    Derived rather than random so it survives a restart when it is based on the
    dashboard token, and so it needs no server-side storage.
    """
    secret = settings.dashboard_token or _PROCESS_SECRET
    return hmac.new(secret.encode(), _DERIVATION_LABEL, sha256).hexdigest()


def _origin_is_same(request: Request) -> bool:
    """Whether the request originated from the dashboard itself.

    Absent Origin AND Referer is treated as acceptable: curl and some
    privacy tools omit both, and the synchronizer token is still required. A
    PRESENT header that disagrees is a refusal -- that is a real cross-site
    submission, not a quiet client.
    """
    stated = request.headers.get("origin") or request.headers.get("referer")
    if not stated:
        return True

    host = request.headers.get("host")
    if not host:
        return False

    return urlsplit(stated).netloc == host


def verify(request: Request, submitted: str | None, settings: Settings) -> str | None:
    """Return a human-readable reason to refuse, or None to proceed."""
    if not _origin_is_same(request):
        return (
            "This request came from another site. If you reached this page from "
            "a link somewhere else, open the dashboard directly and try again."
        )

    expected = issue_token(settings)
    if not submitted or not hmac.compare_digest(submitted, expected):
        return (
            "This form has expired or was not submitted from the dashboard. "
            "Reload the page and try again."
        )
    return None


__all__ = ["_FIELD", "issue_token", "verify"]
