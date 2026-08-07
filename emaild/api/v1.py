"""API v1 routes.

Phase 3 ships authentication only. POST /v1/emails arrives in Phase 4; until
then `/v1/me` exists so an integration can prove its credentials work before
there is anything to send.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from emaild.auth import Principal, require_principal

router = APIRouter(prefix="/v1", tags=["v1"])

# Annotated form rather than a `Depends(...)` default: same behaviour, but it is
# a type annotation instead of a mutable-looking default argument.
AuthedPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get("/me")
async def whoami(principal: AuthedPrincipal) -> dict[str, object]:
    """Echo back what this key is allowed to do.

    Not part of Resend's surface, but the cheapest possible smoke test: it turns
    "is my key configured correctly?" into one curl, and answers questions 1-3 of
    vision.md's authorization model in a form a human can read.

    Returns no secrets -- only the key's name, its project, and the sender
    identities it may use.
    """
    return {
        "project": principal.project_name,
        "key_name": principal.key_name,
        "allowed_senders": principal.allowed_addresses,
        "allowed_domains": sorted(principal.allowed_domains),
    }
