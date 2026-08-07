"""Phase 6 suppression tests.

The auto-suppression boundary gets the most attention here. A wrong entry
silently stops legitimate mail to a real person, and nobody notices until they
complain -- so the tests assert what must NOT be suppressed at least as hard as
what must.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.config import Settings
from emaild.db import dispose_engine, init_engine
from emaild.delivery.bounce import should_suppress, suppression_reason
from emaild.delivery.sink import SinkAdapter
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
    Suppression,
    SuppressionSource,
)
from emaild.suppressions import (
    InvalidAddress,
    add_suppression,
    find_suppressed,
    is_suppressed,
    normalise_address,
    remove_suppression,
)
from emaild.worker import Worker

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
FERNET = "Bb1kMh0e_pJKtxJm5RXFF8pmvWZ_XYbFxK1hHYnQGTk="  # independent throwaway


# --- normalisation (pure) --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("User@Example.COM", "user@example.com"),
        ("  spaced@example.com  ", "spaced@example.com"),
        ("Display Name <user@example.com>", "user@example.com"),
        ('"Quoted, Name" <a@b.co.uk>', "a@b.co.uk"),
    ],
)
def test_normalisation_matches_what_ingest_does(raw, expected):
    """If these diverge, a suppressed address written one way silently fails to
    block the same address written another."""
    assert normalise_address(raw) == expected


def test_plus_addressing_is_not_folded():
    """`a+tag@x.com` and `a@x.com` are different mailboxes to every receiving
    server. Folding them would suppress mail nobody asked to stop."""
    assert normalise_address("a+tag@x.com") != normalise_address("a@x.com")


@pytest.mark.parametrize("bad", ["", "no-at-sign", "@example.com", "user@localhost", "user@.com"])
def test_invalid_addresses_are_rejected(bad):
    with pytest.raises(InvalidAddress):
        normalise_address(bad)


# --- the auto-suppression boundary (pure) ----------------------------------


@pytest.mark.parametrize(
    "response",
    [
        "No such recipient here",
        "550 User unknown",
        "Mailbox does not exist",
        "Recipient address rejected: does not exist",
    ],
)
def test_dead_address_evidence_triggers_suppression(response):
    assert should_suppress(550, response, FailureClass.RECIPIENT_REJECTED) is True


def test_deferral_never_suppresses_even_with_matching_wording():
    """4xx means the server is busy, not that the address is gone."""
    assert (
        should_suppress(450, "Mailbox unavailable, try later", FailureClass.RECIPIENT_REJECTED)
        is False
    )


@pytest.mark.parametrize(
    ("code", "response", "klass"),
    [
        (550, "Message rejected due to spam content", FailureClass.POLICY_REJECTED),
        (550, "Blocked by reputation filter", FailureClass.POLICY_REJECTED),
        (535, "Incorrect authentication data", FailureClass.AUTH_FAILURE),
        (550, "Envelope sender must match your login", FailureClass.SENDER_MISMATCH),
        (550, "only accepted from authorized IP ranges", FailureClass.SENDER_UNAUTHORIZED),
        (552, "Message too large", FailureClass.MESSAGE_INVALID),
    ],
)
def test_failures_about_us_never_suppress_the_recipient(code, response, klass):
    """Suppressing a customer because WE misconfigured the sender, or because
    our reputation dipped, would be absurd -- and invisible."""
    assert should_suppress(code, response, klass) is False


def test_vague_permanent_failure_does_not_suppress():
    """A missing suppression costs one wasted send. A wrong one costs a customer
    who never hears from us again."""
    assert should_suppress(550, "Delivery failed", FailureClass.RECIPIENT_REJECTED) is False


def test_reason_is_actionable_months_later():
    reason = suppression_reason(550, "No such recipient here")
    assert "550" in reason and "No such recipient" in reason and reason.startswith("auto:")


# --- database-backed -------------------------------------------------------


@pytest.fixture
async def env():
    if not TEST_DSN:
        pytest.skip("EMAILD_TEST_DATABASE_URL not set")
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
            domain_id=domain.id, address="noreply@example.com", password_encrypted="x"
        )
        session.add(mailbox)
        await session.commit()
        yield session, project, mailbox
    await engine.dispose()
    await dispose_engine()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        role="worker",
        database_url=TEST_DSN or "postgresql+asyncpg://x/y",
        mailbox_encryption_key=FERNET,
    )


async def test_add_is_idempotent(env):
    """Re-suppressing must not error -- the caller's intent is already satisfied
    and failing would make retry logic pointlessly awkward."""
    session, *_ = env
    _, created_first = await add_suppression(session, "dead@example.net")
    await session.commit()
    _, created_second = await add_suppression(session, "DEAD@Example.NET")
    await session.commit()
    assert created_first is True and created_second is False
    assert len((await session.execute(select(Suppression))).scalars().all()) == 1


async def test_lookup_is_case_and_display_name_insensitive(env):
    session, *_ = env
    await add_suppression(session, "dead@example.net")
    await session.commit()
    assert await is_suppressed(session, "DEAD@EXAMPLE.NET") is True
    assert await is_suppressed(session, "Someone <dead@example.net>") is True
    assert await is_suppressed(session, "alive@example.net") is False


async def test_find_suppressed_handles_a_batch_and_ignores_junk(env):
    session, *_ = env
    await add_suppression(session, "a@example.net")
    await add_suppression(session, "b@example.net")
    await session.commit()
    found = await find_suppressed(
        session, ["A@example.net", "c@example.net", "not-an-address", "b@example.net"]
    )
    assert found == {"a@example.net", "b@example.net"}


async def test_remove_resumes_sending(env):
    session, *_ = env
    await add_suppression(session, "dead@example.net")
    await session.commit()
    assert await remove_suppression(session, "DEAD@example.net") is True
    await session.commit()
    assert await is_suppressed(session, "dead@example.net") is False


# --- auto-suppression through the worker -----------------------------------


async def _queue(session, project, mailbox, recipients: list[str]) -> Message:
    m = Message(
        public_id=f"email_{datetime.now(UTC).timestamp()}",
        project_id=project.id,
        mailbox_id=mailbox.id,
        from_address=mailbox.address,
        to_addresses=recipients,
        body_text="hello",
        recipient_count=len(recipients),
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(m)
    await session.commit()
    return m


async def test_worker_suppresses_a_recipient_the_provider_says_is_dead(env):
    session, project, mailbox = env
    msg = await _queue(session, project, mailbox, ["good@example.net", "dead@example.net"])

    sink = SinkAdapter(fail_recipients={"dead@example.net": (550, "No such recipient here")})
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)

    assert await is_suppressed(session, "dead@example.net") is True
    assert await is_suppressed(session, "good@example.net") is False

    events = (
        (await session.execute(select(Event).where(Event.message_id == msg.id))).scalars().all()
    )
    suppressed = [e for e in events if e.event_type == "recipient.suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0].detail["address"] == "dead@example.net"

    record = (
        await session.execute(select(Suppression).where(Suppression.address == "dead@example.net"))
    ).scalar_one()
    assert record.source is SuppressionSource.BOUNCE


async def test_worker_does_not_suppress_on_policy_rejection(env):
    """A reputation block is about us, not about whether the address exists."""
    session, project, mailbox = env
    msg = await _queue(session, project, mailbox, ["someone@example.net"])
    sink = SinkAdapter(fail_with=(550, "Message rejected due to spam content"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)
    assert await is_suppressed(session, "someone@example.net") is False


async def test_worker_does_not_suppress_on_our_own_misconfiguration(env):
    session, project, mailbox = env
    msg = await _queue(session, project, mailbox, ["someone@example.net"])
    sink = SinkAdapter(fail_with=(550, "Envelope sender x must match your login y"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)
    assert await is_suppressed(session, "someone@example.net") is False


async def test_whole_message_failure_only_suppresses_when_unambiguous(env):
    """With several recipients and no per-recipient detail, we cannot tell which
    address the rejection was about -- so we suppress none of them."""
    session, project, mailbox = env
    msg = await _queue(session, project, mailbox, ["a@example.net", "b@example.net"])
    sink = SinkAdapter(fail_with=(550, "No such recipient here"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)
    assert await find_suppressed(session, ["a@example.net", "b@example.net"]) == set()


async def test_single_recipient_failure_is_unambiguous_and_suppresses(env):
    session, project, mailbox = env
    msg = await _queue(session, project, mailbox, ["only@example.net"])
    sink = SinkAdapter(fail_with=(550, "No such recipient here"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)
    assert await is_suppressed(session, "only@example.net") is True


async def test_suppression_then_blocks_the_next_send(env):
    """End to end: the auto-suppression actually feeds back into ingest."""
    session, project, mailbox = env
    msg = await _queue(session, project, mailbox, ["dead@example.net"])
    sink = SinkAdapter(fail_with=(550, "No such recipient here"))
    await Worker(_settings(), adapter_override=sink)._deliver_one(msg.id)
    await session.commit()

    found = await find_suppressed(session, ["dead@example.net"])
    assert found == {"dead@example.net"}
    # And the message itself ended terminally, not stuck retrying.
    await session.refresh(msg)
    assert msg.status is MessageStatus.PERMANENTLY_REJECTED
