"""Message ingestion.

The ordering here is the reliability guarantee, and it is the whole reason the
API exists in this shape:

    validate -> authorize -> limits -> suppression -> COMMIT -> respond

The response is sent only after the transaction commits. There is no in-memory
handoff, no background task spawned before the write, and no "accepted" returned
on the strength of an intention. If we said `queued`, the row exists.

Everything that can be known to be impossible is rejected *before* the write.
A queued message that violates a hard SMTP limit is worse than a 4xx: it fails
later, asynchronously, in a place the caller is no longer watching.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from emaild.api.schemas import MAX_MESSAGE_BYTES, MAX_RECIPIENTS, SendEmailRequest
from emaild.auth import Principal, authorize_sender
from emaild.errors import ApiError, SuppressedRecipient, ValidationError
from emaild.models import Event, IdempotencyKey, Message, MessageStatus
from emaild.suppressions import find_suppressed

log = logging.getLogger(__name__)

PUBLIC_ID_PREFIX = "email_"


class IdempotencyConflict(ApiError):
    """409 -- same key, still in flight elsewhere.

    Honest answer to a genuine race: we cannot return the first request's result
    because it does not exist yet, and we must not create a second message.
    """

    status_code = 409
    error_type = "idempotency_conflict"


class IdempotencyKeyReused(ApiError):
    """422 -- same key, different payload.

    Silently replaying the old response here would be the dangerous failure:
    the caller believes their new message was sent when it never existed.
    """

    status_code = 422
    error_type = "idempotency_key_reused"


@dataclass
class IngestResult:
    public_id: str
    status: str
    replayed: bool = False


def new_public_id() -> str:
    """`email_01J...` -- ULID, time-ordered to the millisecond.

    The leading 48 bits are a timestamp, so ids created in different
    milliseconds sort in creation order. Within a single millisecond the
    remaining bits are random and ordering is arbitrary -- ULID is not
    monotonic unless a monotonic generator is used, and we do not need one.

    What we actually want is index locality: message tables are queried by
    recency constantly, and a timestamp-prefixed id keeps those scans clustered.
    Millisecond granularity delivers that completely. Do not rely on these ids
    for strict ordering of same-millisecond messages -- use `created_at`, or the
    `events.sequence` column, both of which mean it.
    """
    return f"{PUBLIC_ID_PREFIX}{ULID()}"


def message_id_header(public_id: str, domain: str) -> str:
    """The RFC 5322 Message-ID we author and control.

    This is the bounce-attribution key. VERP is unavailable -- MXRoute pins the
    envelope sender to the login and rejects plus-addressing (spike_results.md,
    Finding 1a) -- so a DSN quoting the original Message-ID is the only thread
    back to the message that caused it. `public_id` is already unique and
    indexed, so that lookup is free.
    """
    return f"<{public_id}@{domain}>"


def _normalise_recipients(values: list[str], field: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        _, address = parseaddr(raw or "")
        address = address.strip().lower()
        local, _, domain = address.rpartition("@")
        if not local or not domain or "." not in domain or domain.startswith("."):
            raise ValidationError(f"Invalid recipient address: {raw!r}", param=field)
        # De-duplicate: the same address twice is one delivery, and counting it
        # twice would burn recipient budget for nothing.
        if address not in seen:
            seen.add(address)
            out.append(address)
    return out


def canonical_request_hash(payload: dict) -> str:
    """Stable fingerprint of a request body, for idempotency-key comparison.

    Sorted keys so that key order in the caller's JSON does not make two
    identical requests look different.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


async def _check_suppressions(session: AsyncSession, recipients: list[str]) -> None:
    # Uses the shared normaliser rather than a local comparison: a suppressed
    # address stored one way must block the same address sent another way.
    rows = await find_suppressed(session, recipients)
    if rows:
        # Refusing the whole request rather than silently dropping the suppressed
        # recipients. Partial delivery reported as success is exactly the theatre
        # vision.md exists to avoid -- the caller can remove them and retry.
        raise SuppressedRecipient(
            f"Refusing to send to suppressed address(es): {', '.join(sorted(rows))}.",
            param="to",
        )


async def _replay_if_seen(
    session: AsyncSession, project_id: int, key: str, request_hash: str
) -> IngestResult | None:
    existing = (
        await session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.project_id == project_id, IdempotencyKey.key == key
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return None

    if existing.request_hash != request_hash:
        raise IdempotencyKeyReused(
            f"Idempotency-Key {key!r} was already used with a different request body. "
            "Use a new key for a different message.",
            param="Idempotency-Key",
        )

    if existing.response_body is None:
        # The original request is still inside its transaction.
        raise IdempotencyConflict(
            f"A request with Idempotency-Key {key!r} is currently in progress. Retry shortly.",
            param="Idempotency-Key",
        )

    body = existing.response_body
    return IngestResult(
        public_id=str(body.get("id")), status=str(body.get("status")), replayed=True
    )


async def ingest_message(
    session: AsyncSession,
    principal: Principal,
    request: SendEmailRequest,
    *,
    idempotency_key: str | None,
    body_retention_hours: int,
    idempotency_ttl_hours: int,
) -> IngestResult:
    """Accept a message, or explain precisely why it cannot be accepted."""

    # --- 1. Authorization: who, which domain, which identity (Phase 3) -------
    display_name, mailbox = authorize_sender(principal, request.from_)

    # --- 2. Recipients and hard provider limits -----------------------------
    to = _normalise_recipients(request.to if isinstance(request.to, list) else [], "to")
    cc = _normalise_recipients(request.cc if isinstance(request.cc, list) else [], "cc")
    bcc = _normalise_recipients(request.bcc if isinstance(request.bcc, list) else [], "bcc")

    total = len(to) + len(cc) + len(bcc)
    if total == 0:
        raise ValidationError("At least one recipient is required.", param="to")
    if total > MAX_RECIPIENTS:
        raise ValidationError(
            f"{total} recipients exceeds the provider limit of {MAX_RECIPIENTS} per message.",
            param="to",
        )

    size = request.estimated_size_bytes()
    if size > MAX_MESSAGE_BYTES:
        raise ValidationError(
            f"Message is approximately {size:,} bytes, over the {MAX_MESSAGE_BYTES:,} byte limit.",
            param="html",
        )

    if request.reply_to:
        _normalise_recipients([request.reply_to], "reply_to")

    # --- 3. Suppression -----------------------------------------------------
    await _check_suppressions(session, to + cc + bcc)

    # --- 4. Idempotency replay ---------------------------------------------
    payload = request.model_dump(by_alias=True, exclude_none=True)
    request_hash = canonical_request_hash(payload)
    if idempotency_key:
        replay = await _replay_if_seen(session, principal.project_id, idempotency_key, request_hash)
        if replay is not None:
            return replay

    # --- 5. Durable write. Everything below is one transaction. -------------
    public_id = new_public_id()
    domain_name = mailbox.address.split("@", 1)[1]
    now = datetime.now(UTC)

    message = Message(
        public_id=public_id,
        project_id=principal.project_id,
        mailbox_id=mailbox.id,
        api_key_id=principal.api_key_id,
        status=MessageStatus.QUEUED,
        from_address=mailbox.address,
        from_name=display_name,
        to_addresses=to,
        cc_addresses=cc or None,
        bcc_addresses=bcc or None,
        reply_to=request.reply_to,
        subject=request.subject,
        # Forced to the mailbox address: MXRoute permits nothing else.
        return_path=mailbox.address,
        body_html=request.html,
        body_text=request.text,
        size_bytes=size,
        recipient_count=total,
        attempts=0,
        next_attempt_at=now,
    )
    session.add(message)
    await session.flush()

    session.add_all(
        [
            Event(
                message_id=message.id,
                sequence=1,
                event_type="api.accepted",
                detail={
                    "project": principal.project_name,
                    "key": principal.key_name,
                    "recipients": total,
                    "size_bytes": size,
                },
            ),
            Event(
                message_id=message.id,
                sequence=2,
                event_type="message.queued",
                detail={"message_id_header": message_id_header(public_id, domain_name)},
            ),
        ]
    )

    if idempotency_key:
        session.add(
            IdempotencyKey(
                project_id=principal.project_id,
                key=idempotency_key,
                request_hash=request_hash,
                message_id=message.id,
                response_status=200,
                response_body={"id": public_id, "status": MessageStatus.QUEUED.value},
                expires_at=now + timedelta(hours=idempotency_ttl_hours),
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            # Another request inserted the same key between our SELECT and this
            # INSERT. It wins; we must not create a second message.
            await session.rollback()
            raise IdempotencyConflict(
                f"A request with Idempotency-Key {idempotency_key!r} is currently in "
                "progress. Retry shortly.",
                param="Idempotency-Key",
            ) from None

    log.info(
        "accepted %s from %s for %d recipient(s), project=%s",
        public_id,
        mailbox.address,
        total,
        principal.project_name,
    )
    return IngestResult(public_id=public_id, status=MessageStatus.QUEUED.value)


async def purge_expired_bodies(session: AsyncSession, retention_hours: int) -> int:
    """Drop bodies from messages that reached a terminal state long enough ago.

    Metadata and the event timeline persist -- they are what answers "where did
    it fail?". The content does not: verification links, password-reset tokens,
    and private correspondence must not become a permanent archive (vision.md).
    """
    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    rows = (
        (
            await session.execute(
                select(Message).where(
                    Message.completed_at.is_not(None),
                    Message.completed_at < cutoff,
                    Message.body_purged_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for message in rows:
        message.body_html = None
        message.body_text = None
        message.body_purged_at = func.now()
    if rows:
        log.info("purged bodies from %d message(s) past retention", len(rows))
    return len(rows)
