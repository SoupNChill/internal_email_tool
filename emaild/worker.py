"""Delivery worker.

Postgres is the queue. Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, and a
reaper returns rows stuck in `sending` past a threshold. Together those satisfy
"a worker crash must not lose a message" with no ack protocol -- just a
timestamp, which is the part that survives a `kill -9`.

Three rules inherited from what the provider actually does:

1. **The rate gate is hard.** Over-limit is a permanent 5xx with no provider
   queue, so a message must never be attempted at the ceiling.
2. **A rate-limit 5xx retries.** It is the one 5xx that must not be terminal.
3. **An unrecognised 5xx re-queues and is flagged.** We would rather send twice
   than lose one, and rather a human reads an unfamiliar response than have the
   system silently decide it was fatal.

Shutdown is graceful (first_production_packaging §14): on SIGTERM the loop stops
claiming, lets in-flight work finish, and exits. Anything still claimed when the
process dies is picked up by the reaper -- never lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from emaild import __version__
from emaild.config import Role, Settings, get_settings
from emaild.crypto import MailboxCipher
from emaild.db import dispose_engine, init_engine, session_scope
from emaild.delivery.base import DeliveryAdapter, DeliveryResult, OutboundMessage
from emaild.delivery.bounce import should_suppress, suppression_reason
from emaild.delivery.classify import Verdict, classify
from emaild.delivery.smtp import ServerLimits, SmtpAdapter
from emaild.ingest import purge_expired_bodies
from emaild.logging_config import configure_logging
from emaild.models import (
    Domain,
    Event,
    Mailbox,
    Message,
    MessageStatus,
    SuppressionSource,
    WorkerHeartbeat,
)
from emaild.ratelimit import current_budget, next_window_opening
from emaild.suppressions import InvalidAddress, add_suppression

log = logging.getLogger(__name__)

# A claim older than this means the worker holding it died. Generous relative to
# the SMTP timeout so a slow-but-alive send is never stolen and duplicated.
STALE_CLAIM_AFTER = timedelta(minutes=15)

POLL_INTERVAL = 5.0
BATCH_SIZE = 10
MAX_ATTEMPTS = 8

# Exponential with jitter. Jitter matters: without it, a provider blip defers a
# batch to the same instant and they all return together, reproducing the
# thundering herd that caused the blip.
_BACKOFF = [30, 120, 600, 1800, 7200, 21600, 43200, 86400]


def backoff_delay(attempt: int) -> timedelta:
    base = _BACKOFF[min(attempt, len(_BACKOFF)) - 1] if attempt > 0 else _BACKOFF[0]
    # Jitter, not a secret. Predictability here costs nothing; a CSPRNG would
    # only be slower.
    return timedelta(seconds=base * random.uniform(0.8, 1.2))  # noqa: S311


class Worker:
    def __init__(self, settings: Settings, adapter_override: DeliveryAdapter | None = None) -> None:
        self.settings = settings
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._shutdown = asyncio.Event()
        self._adapter_override = adapter_override
        self._processed = 0
        self._adapters: dict[int, DeliveryAdapter] = {}  # mailbox_id -> adapter
        self._cipher = (
            MailboxCipher(settings.mailbox_encryption_key)
            if settings.mailbox_encryption_key
            else None
        )

    # -- lifecycle ----------------------------------------------------------

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            log.info("worker: shutdown requested; finishing in-flight work")
            self._shutdown.set()

    async def run(self) -> None:
        log.info("worker %s starting", self.worker_id)
        last_maintenance = datetime.min.replace(tzinfo=UTC)

        while not self._shutdown.is_set():
            try:
                if datetime.now(UTC) - last_maintenance > timedelta(minutes=5):
                    await self._maintenance()
                    last_maintenance = datetime.now(UTC)

                await self._heartbeat()
                processed = await self._process_batch()
                if processed == 0:
                    # Nothing claimable. Wait, but stay responsive to SIGTERM.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._shutdown.wait(), timeout=POLL_INTERVAL)
            except Exception:
                log.exception("worker: unexpected error in main loop")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._shutdown.wait(), timeout=POLL_INTERVAL)

        for adapter in self._adapters.values():
            await adapter.close()
        log.info("worker %s stopped cleanly", self.worker_id)

    async def _heartbeat(self) -> None:
        """Report liveness for a process with no listener.

        Failure here must never stop delivery: the heartbeat is a diagnostic,
        and a worker that refuses to send because it could not write a status
        row would have inverted its own priorities.
        """
        try:
            async with session_scope() as session:
                await session.execute(
                    pg_insert(WorkerHeartbeat)
                    .values(
                        worker_id=self.worker_id,
                        version=__version__,
                        last_seen_at=datetime.now(UTC),
                        messages_processed=self._processed,
                    )
                    .on_conflict_do_update(
                        index_elements=[WorkerHeartbeat.worker_id],
                        set_={
                            "last_seen_at": datetime.now(UTC),
                            "messages_processed": self._processed,
                            "version": __version__,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("worker: heartbeat failed (delivery continues): %s", exc)

    async def _maintenance(self) -> None:
        async with session_scope() as session:
            reaped = await reap_stale_claims(session, STALE_CLAIM_AFTER)
            if reaped:
                log.warning("worker: reaped %d stale claim(s) from dead workers", reaped)
            purged = await purge_expired_bodies(session, self.settings.body_retention_hours)
            if purged:
                log.info("worker: purged %d expired message bodies", purged)

    # -- the loop -----------------------------------------------------------

    async def _process_batch(self) -> int:
        async with session_scope() as session:
            claimed = await claim_messages(session, self.worker_id, BATCH_SIZE)
            if not claimed:
                return 0
            ids = [m.id for m in claimed]

        processed = 0
        for message_id in ids:
            if self._shutdown.is_set():
                # Release the rest rather than holding claims we will not serve.
                async with session_scope() as session:
                    await release_claim(session, message_id)
                continue
            await self._deliver_one(message_id)
            processed += 1
        return processed

    async def _deliver_one(self, message_id: int) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Message, Mailbox, Domain)
                    .join(Mailbox, Message.mailbox_id == Mailbox.id)
                    .join(Domain, Mailbox.domain_id == Domain.id)
                    .where(Message.id == message_id)
                )
            ).one_or_none()
            if row is None:
                return
            message, mailbox, domain = row

            # --- the hard gate ---------------------------------------------
            budget = await current_budget(session, mailbox, self.settings.rate_limit_safety_margin)
            if budget.exhausted:
                opening = await next_window_opening(session, mailbox.id)
                message.status = MessageStatus.QUEUED
                message.claimed_by = None
                message.claimed_at = None
                message.next_attempt_at = opening
                await _emit(
                    session,
                    message,
                    "delivery.rate_gated",
                    {
                        "used": budget.used,
                        "our_ceiling": budget.ceiling,
                        "provider_ceiling": budget.provider_ceiling,
                        "retry_at": opening.isoformat(),
                    },
                )
                log.warning(
                    "rate gate held %s: %d/%d used this hour, retry at %s",
                    message.public_id,
                    budget.used,
                    budget.ceiling,
                    opening.isoformat(),
                )
                return

            if domain.smtp_host is None:
                await self._fail_permanently(
                    session, message, None, "no SMTP host known for domain", needs_review=True
                )
                return

            outbound = OutboundMessage(
                public_id=message.public_id,
                envelope_from=message.return_path or mailbox.address,
                from_address=message.from_address,
                from_name=message.from_name,
                to=list(message.to_addresses or []),
                cc=list(message.cc_addresses or []),
                bcc=list(message.bcc_addresses or []),
                reply_to=message.reply_to,
                subject=message.subject,
                html=message.body_html,
                text=message.body_text,
            )
            host = domain.smtp_host
            adapter = await self._adapter_for(mailbox)

            # Stamp the attempt BEFORE the wire. If this process dies mid-send,
            # the attempt still counts against the hourly budget -- assuming it
            # did not would risk overshooting a ceiling that rejects rather than
            # defers.
            message.last_attempt_at = datetime.now(UTC)
            message.attempts += 1
            attempt_no = message.attempts
            await _emit(session, message, "delivery.attempt", {"attempt": attempt_no, "host": host})

        # The SMTP conversation happens OUTSIDE the transaction. Holding a
        # database transaction open across a network round trip would pin
        # connections for the duration of every send.
        started = time.monotonic()
        result = await adapter.send(outbound, host=host)
        latency_ms = int((time.monotonic() - started) * 1000)

        async with session_scope() as session:
            message = (
                await session.execute(select(Message).where(Message.id == message_id))
            ).scalar_one()
            message.provider_latency_ms = latency_ms
            await self._record_outcome(session, message, result, attempt_no)
        self._processed += 1

    # -- outcome handling ---------------------------------------------------

    async def _record_outcome(
        self, session: AsyncSession, message: Message, result: DeliveryResult, attempt: int
    ) -> None:
        if result.success:
            message.status = MessageStatus.ACCEPTED_BY_PROVIDER
            message.completed_at = func.now()
            message.provider_response = (result.response or "")[:500]
            message.failure_class = None
            message.failure_code = None
            message.claimed_by = None
            message.claimed_at = None

            detail: dict[str, object] = {
                "code": result.code,
                "response": (result.response or "")[:200],
                "accepted": len(result.accepted_recipients),
            }
            if result.partial:
                # Honesty: some recipients were refused. Say which, rather than
                # reporting an unqualified success.
                detail["refused"] = {a: f"{c} {t}"[:120] for a, (c, t) in result.refused.items()}
                await _emit(session, message, "delivery.partial", detail)
            await _emit(session, message, "provider.accepted", detail)
            log.info(
                "delivered %s to %d recipient(s)%s",
                message.public_id,
                len(result.accepted_recipients),
                f" ({len(result.refused)} refused)" if result.refused else "",
            )
            # A partial success still names dead addresses -- and those per-
            # recipient refusals are the clearest evidence we ever get. Skipping
            # them here would mean the strongest signal is the one we ignore.
            await self._suppress_dead_recipients(session, message, result)
            return

        verdict = classify(result.code, result.response)
        await self._apply_failure(session, message, result, verdict, attempt)
        await self._suppress_dead_recipients(session, message, result)

    async def _suppress_dead_recipients(
        self,
        session: AsyncSession,
        message: Message,
        result: DeliveryResult,
    ) -> None:
        """Record addresses the provider told us do not exist.

        The only bounce signal available synchronously. Deliberately narrow --
        see delivery/bounce.py for why the bar is set where it is.
        """
        candidates: list[tuple[str, int | None, str | None]] = [
            (addr, code, text) for addr, (code, text) in result.refused.items()
        ]
        if not candidates and not result.success:
            # A whole-message failure with no per-recipient detail. Only act when
            # the message had exactly one recipient, since otherwise we cannot
            # tell which address the rejection was about.
            recipients = list(message.to_addresses or []) + list(message.cc_addresses or [])
            if len(recipients) == 1 and not message.bcc_addresses:
                candidates = [(recipients[0], result.code, result.response)]

        for address, code, text in candidates:
            # Classify each refusal on its own terms. A message-level verdict
            # would be wrong here: in a partial success the message succeeded
            # while this individual recipient was rejected outright.
            klass = classify(code, text).failure_class
            if not should_suppress(code, text, klass):
                continue
            try:
                _, created = await add_suppression(
                    session,
                    address,
                    source=SuppressionSource.BOUNCE,
                    reason=suppression_reason(code, text),
                )
            except InvalidAddress:
                continue
            if created:
                await _emit(
                    session,
                    message,
                    "recipient.suppressed",
                    {"address": address, "code": code, "response": (text or "")[:160]},
                )
                log.warning("auto-suppressed %s after %s %s", address, code, (text or "")[:80])

    async def _apply_failure(
        self,
        session: AsyncSession,
        message: Message,
        result: DeliveryResult,
        verdict: Verdict,
        attempt: int,
    ) -> None:
        message.failure_class = verdict.failure_class
        message.failure_code = result.code
        message.provider_response = (result.response or "")[:500]
        message.claimed_by = None
        message.claimed_at = None
        if verdict.needs_review:
            message.needs_review = True

        detail: dict[str, object] = {
            "attempt": attempt,
            "code": result.code,
            "response": (result.response or "")[:200],
            "class": verdict.failure_class.value,
            "retryable": verdict.retryable,
        }
        if verdict.note:
            detail["note"] = verdict.note
        if result.refused:
            detail["refused"] = {a: f"{c} {t}"[:120] for a, (c, t) in result.refused.items()}

        if verdict.retryable and attempt < MAX_ATTEMPTS:
            delay = backoff_delay(attempt)
            message.status = MessageStatus.TEMPORARILY_FAILED
            message.next_attempt_at = datetime.now(UTC) + delay
            detail["retry_in_seconds"] = int(delay.total_seconds())
            await _emit(session, message, "delivery.deferred", detail)
            log.warning(
                "deferred %s (%s, attempt %d/%d), retrying in %ds",
                message.public_id,
                verdict.failure_class.value,
                attempt,
                MAX_ATTEMPTS,
                int(delay.total_seconds()),
            )
            return

        if verdict.retryable:
            detail["note"] = f"gave up after {MAX_ATTEMPTS} attempts"
            message.needs_review = True

        message.status = MessageStatus.PERMANENTLY_REJECTED
        message.completed_at = func.now()
        await _emit(session, message, "delivery.failed", detail)
        log.error(
            "failed %s permanently (%s, code=%s)",
            message.public_id,
            verdict.failure_class.value,
            result.code,
        )

    async def _fail_permanently(
        self,
        session: AsyncSession,
        message: Message,
        code: int | None,
        reason: str,
        *,
        needs_review: bool = False,
    ) -> None:
        message.status = MessageStatus.PERMANENTLY_REJECTED
        message.completed_at = func.now()
        message.failure_code = code
        message.provider_response = reason[:500]
        message.needs_review = needs_review
        message.claimed_by = None
        message.claimed_at = None
        await _emit(session, message, "delivery.failed", {"reason": reason, "code": code})

    # -- adapters -----------------------------------------------------------

    async def _adapter_for(self, mailbox: Mailbox) -> DeliveryAdapter:
        if self._adapter_override is not None:
            return self._adapter_override
        if mailbox.id not in self._adapters:
            if self._cipher is None:
                raise RuntimeError("worker requires EMAILD_MAILBOX_ENCRYPTION_KEY to send")
            self._adapters[mailbox.id] = SmtpAdapter(
                username=mailbox.address,
                password=self._cipher.decrypt(mailbox.password_encrypted),
                port=self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
                fallback_limits=ServerLimits(
                    self.settings.fallback_max_messages_per_connection,
                    self.settings.fallback_max_recipients,
                    self.settings.fallback_max_message_bytes,
                ),
            )
        return self._adapters[mailbox.id]


# --- queue primitives ------------------------------------------------------


async def claim_messages(session: AsyncSession, worker_id: str, limit: int) -> list[Message]:
    """Atomically take up to `limit` due messages.

    SKIP LOCKED is what lets several workers share one table without
    coordinating: each takes rows nobody else has locked, and no row is ever
    handed out twice.
    """
    now = datetime.now(UTC)
    subquery = (
        select(Message.id)
        .where(
            Message.status.in_([MessageStatus.QUEUED, MessageStatus.TEMPORARILY_FAILED]),
            Message.next_attempt_at <= now,
        )
        .order_by(Message.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = (await session.execute(subquery)).scalars().all()
    if not ids:
        return []

    await session.execute(
        update(Message)
        .where(Message.id.in_(ids))
        .values(status=MessageStatus.SENDING, claimed_by=worker_id, claimed_at=now)
    )
    return list((await session.execute(select(Message).where(Message.id.in_(ids)))).scalars().all())


async def release_claim(session: AsyncSession, message_id: int) -> None:
    """Hand a claimed message back, unattempted. Used during shutdown."""
    await session.execute(
        update(Message)
        .where(Message.id == message_id, Message.status == MessageStatus.SENDING)
        .values(status=MessageStatus.QUEUED, claimed_by=None, claimed_at=None)
    )


async def reap_stale_claims(session: AsyncSession, older_than: timedelta) -> int:
    """Return messages abandoned by dead workers to the queue.

    This is the crash-safety mechanism. A worker killed mid-send leaves a row in
    `sending` forever; the reaper is what makes that recoverable without any
    acknowledgement protocol.
    """
    cutoff = datetime.now(UTC) - older_than
    result = await session.execute(
        update(Message)
        .where(Message.status == MessageStatus.SENDING, Message.claimed_at < cutoff)
        .values(
            status=MessageStatus.TEMPORARILY_FAILED,
            claimed_by=None,
            claimed_at=None,
            next_attempt_at=datetime.now(UTC),
        )
    )
    return int(result.rowcount or 0)


async def _emit(
    session: AsyncSession, message: Message, event_type: str, detail: dict | None = None
) -> None:
    next_sequence = (
        await session.execute(
            select(func.coalesce(func.max(Event.sequence), 0) + 1).where(
                Event.message_id == message.id
            )
        )
    ).scalar_one()
    session.add(
        Event(
            message_id=message.id,
            sequence=next_sequence,
            event_type=event_type,
            detail=detail,
        )
    )


# --- entrypoint ------------------------------------------------------------


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.role is not Role.WORKER:
        raise RuntimeError(
            f"emaild.worker serves role=worker but EMAILD_ROLE={settings.role.value}"
        )

    init_engine(settings)
    worker = Worker(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_shutdown)

    try:
        await worker.run()
    finally:
        await dispose_engine()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
