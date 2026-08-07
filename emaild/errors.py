"""API error model.

One shape for every failure, so an integrator -- or an AI writing integration
code against this API -- never has to guess how an error is spelled:

    {"error": {"type": "...", "message": "...", "param": "..."}}

Messages are written for whoever is debugging at 2am: what was wrong, and what to
do about it. They never contain a key, a password, or a message body.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Base class for errors that become a clean JSON response."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    error_type: str = "invalid_request"

    def __init__(self, message: str, *, param: str | None = None) -> None:
        self.message = message
        self.param = param
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"type": self.error_type, "message": self.message}
        if self.param:
            error["param"] = self.param
        return {"error": error}


class AuthenticationError(ApiError):
    """401 -- the credential is missing, malformed, unknown, or revoked.

    Deliberately does NOT distinguish "no such key" from "revoked key" in the
    message. Both mean the same thing to a legitimate caller (get a working key)
    and telling them apart only helps someone probing.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "authentication_error"


class AuthorizationError(ApiError):
    """403 -- the key is valid but not permitted to do this.

    This one IS specific about what was refused. The key already authenticated,
    so the holder is trusted; telling them exactly which sender identity they
    lack turns a mystery into a one-line fix.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_type = "authorization_error"


class ValidationError(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_type = "validation_error"


class DomainNotReady(ApiError):
    """422 -- authorised, but the sending domain is not in a state to send.

    Separate from AuthorizationError because the fix is different: publish DNS
    records, do not go asking for a wider key.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_type = "domain_not_ready"


class SuppressedRecipient(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_type = "suppressed_recipient"


class RateLimitError(ApiError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "rate_limit_exceeded"


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette types handlers against bare Exception. Narrow explicitly rather
    # than asserting -- asserts vanish under `python -O`, which would turn a
    # mis-registration into an AttributeError inside the error path itself.
    if not isinstance(exc, ApiError):
        raise exc
    headers = {}
    if isinstance(exc, AuthenticationError):
        # RFC 6750: a 401 must say how to authenticate.
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload(), headers=headers)
