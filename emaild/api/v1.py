"""API v1 routes.

Resend-shaped by design (build_plan.md). The delivery-status vocabulary is the
one deliberate divergence, because it reports what we can prove rather than what
sounds reassuring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from emaild.api.schemas import (
    AddSuppressionRequest,
    EventView,
    MessageView,
    SendEmailRequest,
    SendEmailResponse,
    SuppressionListView,
    SuppressionView,
)
from emaild.auth import Principal, require_principal
from emaild.config import get_settings
from emaild.db import session_scope
from emaild.errors import ApiError
from emaild.errors import ValidationError as ApiValidationError
from emaild.ingest import ingest_message
from emaild.models import Message, SuppressionSource
from emaild.suppressions import (
    InvalidAddress,
    add_suppression,
    count_suppressions,
    list_suppressions,
)

router = APIRouter(prefix="/v1", tags=["v1"])

# Annotated form rather than a `Depends(...)` default: same behaviour, but it is
# a type annotation instead of a mutable-looking default argument.
AuthedPrincipal = Annotated[Principal, Depends(require_principal)]


class MessageNotFound(ApiError):
    status_code = 404
    error_type = "not_found"


@router.get("/me")
async def whoami(principal: AuthedPrincipal) -> dict[str, object]:
    """Echo back what this key is allowed to do.

    Not part of Resend's surface, but the cheapest possible smoke test: it turns
    "is my key configured correctly?" into one curl, and answers questions 1-3 of
    vision.md's authorization model in a form a human can read.
    """
    return {
        "project": principal.project_name,
        "key_name": principal.key_name,
        "allowed_senders": principal.allowed_addresses,
        "allowed_domains": sorted(principal.allowed_domains),
    }


@router.post("/emails", response_model=SendEmailResponse)
async def send_email(
    request: SendEmailRequest,
    principal: AuthedPrincipal,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SendEmailResponse:
    """Accept a message for delivery.

    Returns only after the message is durably committed. `queued` means the row
    exists and a worker will pick it up -- not that anything has been sent, and
    certainly not that anything has been delivered.
    """
    settings = get_settings()
    async with session_scope() as session:
        result = await ingest_message(
            session,
            principal,
            request,
            idempotency_key=idempotency_key,
            body_retention_hours=settings.body_retention_hours,
            idempotency_ttl_hours=settings.idempotency_ttl_hours,
        )
    return SendEmailResponse(id=result.public_id, status=result.status)


@router.get("/emails/{email_id}", response_model=MessageView)
async def get_email(email_id: str, principal: AuthedPrincipal) -> MessageView:
    """A message and its honest timeline.

    Scoped to the caller's project: a key must never be able to read another
    project's mail, and an id from elsewhere returns 404 rather than 403 so the
    endpoint cannot be used to probe which ids exist.
    """
    async with session_scope() as session:
        message = (
            await session.execute(
                select(Message)
                .where(
                    Message.public_id == email_id,
                    Message.project_id == principal.project_id,
                )
                .options(selectinload(Message.events))
            )
        ).scalar_one_or_none()

        if message is None:
            raise MessageNotFound(f"No message with id {email_id!r}.")

        events = [
            EventView(
                type=e.event_type,
                occurred_at=e.occurred_at.isoformat(),
                detail=e.detail,
            )
            for e in sorted(message.events, key=lambda e: e.sequence)
        ]

        return MessageView(
            id=message.public_id,
            status=message.status.value,
            from_address=message.from_address,
            to=list(message.to_addresses or []),
            cc=list(message.cc_addresses) if message.cc_addresses else None,
            bcc=list(message.bcc_addresses) if message.bcc_addresses else None,
            subject=message.subject,
            created_at=message.created_at.isoformat(),
            completed_at=message.completed_at.isoformat() if message.completed_at else None,
            attempts=message.attempts,
            failure_class=message.failure_class.value if message.failure_class else None,
            failure_code=message.failure_code,
            provider_response=message.provider_response,
            events=events,
        )


# --- suppressions ----------------------------------------------------------
#
# The permissions here are deliberately asymmetric.
#
# Adding a suppression fails CLOSED: the worst case is that we stop mailing
# someone we could have mailed. Any API key may do it.
#
# Removing one fails OPEN: it resumes mail to an address we previously had
# reason to distrust, and at worst re-starts sending to a dead address that
# damages reputation on shared IPs. That direction is operator-only, via the
# admin CLI. See emaild/suppressions.py.


@router.get("/suppressions", response_model=SuppressionListView)
async def get_suppressions(
    principal: AuthedPrincipal, limit: int = 100, offset: int = 0
) -> SuppressionListView:
    async with session_scope() as session:
        rows = await list_suppressions(session, limit=limit, offset=offset)
        total = await count_suppressions(session)
    return SuppressionListView(
        total=total,
        data=[
            SuppressionView(
                address=r.address,
                source=r.source.value,
                reason=r.reason,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
    )


@router.post("/suppressions", response_model=SuppressionView, status_code=201)
async def create_suppression(
    request: AddSuppressionRequest, principal: AuthedPrincipal
) -> SuppressionView:
    """Suppress an address. Idempotent -- re-suppressing is a no-op, not an error."""
    async with session_scope() as session:
        try:
            record, _created = await add_suppression(
                session,
                request.address,
                source=SuppressionSource.MANUAL,
                reason=request.reason or f"added via API by project {principal.project_name}",
            )
        except InvalidAddress as exc:
            raise ApiValidationError(str(exc), param="address") from None
        return SuppressionView(
            address=record.address,
            source=record.source.value,
            reason=record.reason,
            created_at=record.created_at.isoformat()
            if record.created_at
            else datetime.now(UTC).isoformat(),
        )
