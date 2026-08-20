"""Form parsing and one-shot messages for the dashboard.

Two small pieces of machinery that the mutation routes need and that would
otherwise be duplicated across them.

**Form parsing without python-multipart.** Starlette's `request.form()` asserts
that library is installed even for `application/x-www-form-urlencoded`, which is
all the dashboard sends -- there are no file uploads. release_rules §18
discourages taking a dependency for something the standard library already does,
and `parse_qsl` does exactly this.

**One-shot values.** A created API key is shown once and never recoverable, so
it cannot be passed through a redirect as a query parameter: it would sit in
browser history, in any proxy log, and in the Referer header sent to the next
site visited. Instead the value stays on the server under a random handle, and
the redirect carries only the handle, which is consumed on first read.

That also gives the mutations Post/Redirect/Get: without it, a refresh after
creating a key would silently create a second one.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Request

# Long enough to survive a redirect and a slow render; short enough that a
# forgotten browser tab is not holding a plaintext key indefinitely.
_TTL_SECONDS = 120

# handle -> (expires_at, payload). Process-local, which is correct: the API runs
# as a single uvicorn process, and these values are deliberately ephemeral. They
# must never reach the database -- a "shown once" secret that is persisted is
# simply a stored secret.
_ONE_SHOT: dict[str, tuple[float, dict[str, Any]]] = {}


async def parse_form(request: Request) -> dict[str, list[str]]:
    """Read an urlencoded form body into {field: [values]}.

    Lists rather than scalars because the key-creation form has a repeated
    checkbox field (one sender identity per box), and collapsing that to a
    single value would silently drop every scope but one.
    """
    raw = await request.body()
    parsed: dict[str, list[str]] = {}
    for key, value in parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True):
        parsed.setdefault(key, []).append(value)
    return parsed


def one(form: dict[str, list[str]], field: str) -> str:
    """The single value of a field, or empty string. Never raises."""
    values = form.get(field) or []
    return values[0].strip() if values else ""


def many(form: dict[str, list[str]], field: str) -> list[str]:
    return [v.strip() for v in (form.get(field) or []) if v.strip()]


def _expire() -> None:
    now = time.monotonic()
    for handle in [h for h, (expires, _) in _ONE_SHOT.items() if expires < now]:
        _ONE_SHOT.pop(handle, None)


def stash(payload: dict[str, Any]) -> str:
    """Store a payload for exactly one retrieval. Returns its handle."""
    _expire()
    handle = secrets.token_urlsafe(16)
    _ONE_SHOT[handle] = (time.monotonic() + _TTL_SECONDS, payload)
    return handle


def take(handle: str | None) -> dict[str, Any] | None:
    """Retrieve and destroy. A second read of the same handle returns None."""
    if not handle:
        return None
    _expire()
    entry = _ONE_SHOT.pop(handle, None)
    return entry[1] if entry else None
