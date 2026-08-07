"""Phase 5 delivery tests.

The classifier and backoff are pure and tested directly. Everything touching the
queue runs against real Postgres, because SKIP LOCKED, partial indexes, and the
reaper are database behaviour -- a mock would only test the mock.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.config import Settings
from emaild.db import dispose_engine, init_engine
from emaild.delivery.base import OutboundMessage, build_mime
from emaild.delivery.classify import classify
from emaild.delivery.sink import SinkAdapter
from emaild.delivery.smtp import ServerLimits
from emaild.models import (
    Base,
    Domain,
    DomainStatus,
    Event,
    FailureClass,
    Mailbox,
    Message,
    MessageStatus,
    Project,
)
from emaild.ratelimit import current_budget, next_window_opening
from emaild.worker import (
    MAX_ATTEMPTS,
    Worker,
    backoff_delay,
    claim_messages,
    reap_stale_claims,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
# Independently generated throwaway. MUST share no prefix with any real
# EMAILD_MAILBOX_ENCRYPTION_KEY -- do not copy a live key and edit it.
FERNET = "MGXM-GZSH9sWs-nqmJYlf-M1g15tgunZjv0Tmf9KsEU="


# --- classification (pure) -------------------------------------------------


def test_observed_auth_failure_is_permanent_and_flagged():
    v = classify(535, "Incorrect authentication data")
    assert v.failure_class is FailureClass.AUTH_FAILURE
    assert v.retryable is False and v.needs_review is True


def test_observed_recipient_rejection_is_permanent_and_not_flagged():
    v = classify(550, "No such recipient here")
    assert v.failure_class is FailureClass.RECIPIENT_REJECTED
    assert v.retryable is False and v.needs_review is False


def test_observed_sender_mismatch_is_flagged_as_our_bug():
    v = classify(550, "Envelope sender x@y.com must match your login z@y.com")
    assert v.failure_class is FailureClass.SENDER_MISMATCH
    assert v.needs_review is True


@pytest.mark.parametrize(
    "response",
    [
        "550 rate limit exceeded",
        "550 Too many messages this hour",
        "552 sending limit reached",
        "550 message limit exceeded for this account",
        "451 Please try again later",
        "550 Throttled",
    ],
)
def test_rate_limit_5xx_is_the_one_permanent_code_that_retries(response):
    """The single most consequential rule. Over-limit is rejected, not deferred,
    so treating it as terminal destroys legitimate mail."""
    v = classify(int(response[:3]), response)
    assert v.retryable is True
    assert v.failure_class in (FailureClass.RATE_LIMITED, FailureClass.PROVIDER_DEFERRAL)


def test_unrecognised_5xx_requeues_and_flags_rather_than_dropping():
    """We would rather send twice than lose one, and rather a human reads an
    unfamiliar response than have the system decide it was fatal."""
    v = classify(521, "Server does not accept mail (something nobody has seen)")
    assert v.retryable is True
    assert v.needs_review is True
    assert v.failure_class is FailureClass.UNKNOWN


def test_transport_failure_says_nothing_about_the_message():
    v = classify(None, "ConnectionResetError: [Errno 104]")
    assert v.failure_class is FailureClass.CONNECTION
    assert v.retryable is True


def test_4xx_is_retryable_by_definition():
    assert classify(451, "Temporary local problem").retryable is True


# --- backoff ---------------------------------------------------------------


def test_backoff_grows_and_is_jittered():
    """Without jitter a provider blip defers a batch to one instant and they all
    return together, reproducing the herd that caused the blip."""
    first = [backoff_delay(1).total_seconds() for _ in range(50)]
    later = backoff_delay(4).total_seconds()
    assert len(set(first)) > 1  # jittered, not constant
    assert all(24 <= s <= 36 for s in first)  # 30s +/- 20%
    assert later > max(first)


def test_backoff_is_bounded_at_the_last_attempt():
    assert backoff_delay(MAX_ATTEMPTS).total_seconds() <= 86400 * 1.2


# --- MIME ------------------------------------------------------------------


def _outbound(**kw) -> OutboundMessage:
    base = dict(
        public_id="email_01TEST",
        envelope_from="noreply@example.com",
        from_address="noreply@example.com",
        from_name="Acme",
        to=["a@example.net"],
        subject="Hi",
        html="<p>hello</p>",
        text="hello",
    )
    base.update(kw)
    return OutboundMessage(**base)  # type: ignore[arg-type]


def test_bcc_is_in_the_envelope_but_never_in_the_headers():
    """Getting this wrong discloses recipients to each other."""
    msg = _outbound(to=["a@x.com"], cc=["c@x.com"], bcc=["secret@x.com"])
    mime = build_mime(msg, "<email_01TEST@example.com>")
    assert "secret@x.com" not in mime.as_string()
    assert "secret@x.com" in msg.envelope_recipients
    assert mime["Cc"] == "c@x.com"


def test_message_id_is_the_one_we_authored():
    mime = build_mime(_outbound(), "<email_01TEST@example.com>")
    assert mime["Message-ID"] == "<email_01TEST@example.com>"


def test_html_only_still_gets_a_plaintext_part():
    mime = build_mime(_outbound(text=None, html="<p>Hello <b>world</b></p>"), "<x@y.com>")
    assert mime.is_multipart()
    body = mime.get_body(preferencelist=("plain",))
    assert body is not None and "Hello" in body.get_content()


def test_caller_headers_cannot_override_identity_headers():
    """A caller-supplied From: or Message-ID would break DMARC alignment or
    bounce attribution."""
    mime = build_mime(
        _outbound(headers={"From": "evil@attacker.com", "Message-ID": "<forged@x>", "X-Tag": "ok"}),
        "<email_01TEST@example.com>",
    )
    assert "evil@attacker.com" not in str(mime["From"])
    assert mime["Message-ID"] == "<email_01TEST@example.com>"
    assert mime["X-Tag"] == "ok"


def test_ehlo_limits_are_parsed_from_the_banner():
    banner = (
        "chocobo.mxrouting.net Hello\nSIZE 52428800\nLIMITS MAILMAX=100 RCPTMAX=150\n"
        "8BITMIME\nPIPELINING\nAUTH PLAIN LOGIN"
    )
    limits = ServerLimits.from_ehlo(banner, ServerLimits(1, 1, 1))
    assert limits.max_messages_per_connection == 100
    assert limits.max_recipients == 150
    assert limits.max_size_bytes == 52_428_800


def test_ehlo_falls_back_when_the_server_advertises_nothing():
    fallback = ServerLimits(50, 75, 1000)
    assert ServerLimits.from_ehlo("plain hello", fallback) == fallback


# --- database-backed -------------------------------------------------------

pytestmark_db = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")


@pytest.fixture
async def env():
    if not TEST_DSN:
        pytest.skip("EMAILD_TEST_DATABASE_URL not set")
    # The Worker uses the module-level sessionmaker, so the global engine must be
    # initialised too -- not just the one this fixture holds.
    await dispose_engine()
    init_engine(_settings())
    engine = create_async_engine(TEST_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        project = Project(name="p")
        domain = Domain(name="example.com", status=DomainStatus.READY, smtp_host="smtp.example.com")
        session.add_all([project, domain])
        await session.flush()
        mailbox = Mailbox(
            domain_id=domain.id,
            address="noreply@example.com",
            password_encrypted="x",
            hourly_limit=10,
        )
        session.add(mailbox)
        await session.commit()
        yield session, project, mailbox, maker
    await engine.dispose()
    await dispose_engine()


async def _queue(session, project, mailbox, n=1, **kw):
    made = []
    kw.setdefault("next_attempt_at", datetime.now(UTC) - timedelta(seconds=1))
    for i in range(n):
        m = Message(
            public_id=f"email_test{i}_{datetime.now(UTC).timestamp()}",
            project_id=project.id,
            mailbox_id=mailbox.id,
            from_address=mailbox.address,
            to_addresses=["a@example.net"],
            body_text="hello",
            recipient_count=1,
            size_bytes=100,
            **kw,
        )
        session.add(m)
        made.append(m)
    await session.commit()
    return made


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        role="worker",
        database_url=TEST_DSN or "postgresql+asyncpg://x/y",
        mailbox_encryption_key=FERNET,
    )


async def test_claim_marks_sending_and_records_the_worker(env):
    session, project, mailbox, _ = env
    await _queue(session, project, mailbox, n=3)
    claimed = await claim_messages(session, "worker-a", 10)
    await session.commit()
    assert len(claimed) == 3
    assert all(m.status is MessageStatus.SENDING for m in claimed)
    assert all(m.claimed_by == "worker-a" for m in claimed)


async def test_future_messages_are_not_claimed(env):
    session, project, mailbox, _ = env
    await _queue(session, project, mailbox, n=2)
    await _queue(
        session, project, mailbox, n=2, next_attempt_at=datetime.now(UTC) + timedelta(hours=1)
    )
    claimed = await claim_messages(session, "w", 10)
    assert len(claimed) == 2


async def test_reaper_recovers_messages_from_a_dead_worker(env):
    """The crash-safety mechanism: a timestamp, not an ack protocol -- because a
    timestamp survives kill -9."""
    session, project, mailbox, _ = env
    await _queue(session, project, mailbox, n=2)
    await claim_messages(session, "doomed-worker", 10)
    await session.commit()

    # Simulate the worker dying: claims age without ever completing.
    await session.execute(
        Message.__table__.update().values(claimed_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await session.commit()

    reaped = await reap_stale_claims(session, timedelta(minutes=15))
    await session.commit()
    assert reaped == 2

    rows = (await session.execute(select(Message))).scalars().all()
    assert all(r.status is MessageStatus.TEMPORARILY_FAILED for r in rows)
    assert all(r.claimed_by is None for r in rows)


async def test_reaper_leaves_fresh_claims_alone(env):
    """A slow-but-alive send must never be stolen and duplicated."""
    session, project, mailbox, _ = env
    await _queue(session, project, mailbox, n=1)
    await claim_messages(session, "busy-worker", 10)
    await session.commit()
    assert await reap_stale_claims(session, timedelta(minutes=15)) == 0


# --- the rate gate ---------------------------------------------------------


async def test_budget_counts_attempts_not_successes(env):
    """Counting only successes would let a burst of retries sail past a ceiling
    that rejects rather than defers."""
    session, project, mailbox, _ = env
    await _queue(
        session,
        project,
        mailbox,
        n=4,
        last_attempt_at=datetime.now(UTC),
        status=MessageStatus.PERMANENTLY_REJECTED,
    )
    budget = await current_budget(session, mailbox, 0.9)
    assert budget.used == 4
    assert budget.ceiling == 9  # 90% of 10


async def test_budget_ignores_attempts_outside_the_rolling_window(env):
    session, project, mailbox, _ = env
    await _queue(
        session, project, mailbox, n=3, last_attempt_at=datetime.now(UTC) - timedelta(hours=2)
    )
    assert (await current_budget(session, mailbox, 0.9)).used == 0


async def test_gate_closes_before_the_provider_ceiling(env):
    """Our backpressure must engage before theirs, because theirs is a wall."""
    session, project, mailbox, _ = env
    await _queue(session, project, mailbox, n=9, last_attempt_at=datetime.now(UTC))
    budget = await current_budget(session, mailbox, 0.9)
    assert budget.exhausted is True
    assert budget.headroom_to_provider == 1  # still one short of the real limit


async def test_next_opening_is_when_the_oldest_attempt_ages_out(env):
    session, project, mailbox, _ = env
    oldest = datetime.now(UTC) - timedelta(minutes=30)
    await _queue(session, project, mailbox, n=1, last_attempt_at=oldest)
    opening = await next_window_opening(session, mailbox.id)
    assert timedelta(minutes=29) < (opening - datetime.now(UTC)) < timedelta(minutes=31)


async def test_worker_holds_a_message_at_the_gate_without_attempting_it(env):
    """The whole point: at the ceiling, nothing reaches the wire."""
    session, project, mailbox, maker = env
    await _queue(session, project, mailbox, n=9, last_attempt_at=datetime.now(UTC))
    target = (await _queue(session, project, mailbox, n=1))[0]

    sink = SinkAdapter()
    worker = Worker(_settings(), adapter_override=sink)
    await worker._deliver_one(target.id)

    await session.refresh(target)
    assert sink.sent == []  # never attempted
    assert target.status is MessageStatus.QUEUED
    assert target.next_attempt_at > datetime.now(UTC)
    events = (
        (await session.execute(select(Event).where(Event.message_id == target.id))).scalars().all()
    )
    assert any(e.event_type == "delivery.rate_gated" for e in events)


# --- delivery outcomes -----------------------------------------------------


async def test_successful_send_is_accepted_not_delivered(env):
    """`accepted_by_provider` is terminal and does NOT mean delivered -- bad
    external recipients are accepted at RCPT and bounce out of band."""
    session, project, mailbox, _ = env
    msg = (await _queue(session, project, mailbox, n=1))[0]
    sink = SinkAdapter()
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)

    await session.refresh(msg)
    assert msg.status is MessageStatus.ACCEPTED_BY_PROVIDER
    assert msg.completed_at is not None
    assert msg.attempts == 1
    assert len(sink.sent) == 1
    types = [
        e.event_type
        for e in (
            (
                await session.execute(
                    select(Event).where(Event.message_id == msg.id).order_by(Event.sequence)
                )
            )
            .scalars()
            .all()
        )
    ]
    assert "delivery.attempt" in types and "provider.accepted" in types


async def test_retryable_failure_defers_with_backoff(env):
    session, project, mailbox, _ = env
    msg = (await _queue(session, project, mailbox, n=1))[0]
    sink = SinkAdapter(fail_with=(451, "Temporary local problem, try again later"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)

    await session.refresh(msg)
    assert msg.status is MessageStatus.TEMPORARILY_FAILED
    assert msg.next_attempt_at > datetime.now(UTC)
    assert msg.completed_at is None


async def test_permanent_failure_stops_immediately(env):
    session, project, mailbox, _ = env
    msg = (await _queue(session, project, mailbox, n=1))[0]
    sink = SinkAdapter(fail_with=(550, "No such recipient here"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)

    await session.refresh(msg)
    assert msg.status is MessageStatus.PERMANENTLY_REJECTED
    assert msg.failure_class is FailureClass.RECIPIENT_REJECTED
    assert msg.needs_review is False


async def test_unknown_5xx_is_retried_and_flagged_never_dropped(env):
    session, project, mailbox, _ = env
    msg = (await _queue(session, project, mailbox, n=1))[0]
    sink = SinkAdapter(fail_with=(521, "Something nobody has documented"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)

    await session.refresh(msg)
    assert msg.status is MessageStatus.TEMPORARILY_FAILED
    assert msg.needs_review is True


async def test_partial_recipient_rejection_is_recorded_honestly(env):
    """Some accepted, some refused. Reporting an unqualified success would hide
    the difference."""
    session, project, mailbox, _ = env
    msg = Message(
        public_id="email_partial",
        project_id=project.id,
        mailbox_id=mailbox.id,
        from_address=mailbox.address,
        to_addresses=["good@example.net", "bad@example.net"],
        body_text="x",
        recipient_count=2,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(msg)
    await session.commit()

    sink = SinkAdapter(fail_recipients={"bad@example.net": (550, "User unknown")})
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)

    await session.refresh(msg)
    assert msg.status is MessageStatus.ACCEPTED_BY_PROVIDER
    events = {
        e.event_type: e.detail
        for e in (
            (await session.execute(select(Event).where(Event.message_id == msg.id))).scalars().all()
        )
    }
    assert "delivery.partial" in events
    assert "bad@example.net" in events["delivery.partial"]["refused"]


async def test_attempts_are_capped(env):
    session, project, mailbox, _ = env
    msg = (await _queue(session, project, mailbox, n=1, attempts=MAX_ATTEMPTS - 1))[0]
    sink = SinkAdapter(fail_with=(451, "still deferring"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)

    await session.refresh(msg)
    assert msg.attempts == MAX_ATTEMPTS
    assert msg.status is MessageStatus.PERMANENTLY_REJECTED
    assert msg.needs_review is True  # gave up, so a human should see it


async def test_shutdown_releases_unstarted_claims(env):
    """A message claimed but not attempted must go back to the queue, not sit in
    `sending` waiting for the reaper."""
    session, project, mailbox, _ = env
    await _queue(session, project, mailbox, n=5)
    sink = SinkAdapter()
    worker = Worker(_settings(), adapter_override=sink)
    worker.request_shutdown()

    processed = await worker._process_batch()
    await asyncio.sleep(0)

    rows = (await session.execute(select(Message))).scalars().all()
    for r in rows:
        await session.refresh(r)
    assert processed == 0
    assert sink.sent == []
    assert all(r.status is MessageStatus.QUEUED for r in rows)
    assert (
        await session.execute(
            select(func.count(Message.id)).where(Message.status == MessageStatus.SENDING)
        )
    ).scalar_one() == 0
