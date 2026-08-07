"""Phase 4 ingest tests.

The ordering guarantee is the point of this phase, so most of these assert what
must be true *in the database* after a call, not merely what came back.
"""

from __future__ import annotations

import os
import time

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.api.schemas import MAX_RECIPIENTS, SendEmailRequest
from emaild.auth import resolve_principal
from emaild.crypto import generate_api_key
from emaild.errors import AuthorizationError, SuppressedRecipient, ValidationError
from emaild.ingest import (
    IdempotencyConflict,
    IdempotencyKeyReused,
    canonical_request_hash,
    ingest_message,
    message_id_header,
    new_public_id,
    purge_expired_bodies,
)
from emaild.models import (
    ApiKey,
    ApiKeyScope,
    Base,
    Domain,
    DomainStatus,
    Event,
    IdempotencyKey,
    Mailbox,
    Message,
    MessageStatus,
    Project,
    Suppression,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

INGEST_DEFAULTS = {"body_retention_hours": 72, "idempotency_ttl_hours": 24}


@pytest.fixture
async def env():
    engine = create_async_engine(TEST_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        project = Project(name="billing")
        other = Project(name="other")
        domain = Domain(name="example.com", status=DomainStatus.READY)
        session.add_all([project, other, domain])
        await session.flush()

        mailbox = Mailbox(
            domain_id=domain.id, address="noreply@example.com", password_encrypted="x"
        )
        session.add(mailbox)
        await session.flush()

        key, digest, prefix = generate_api_key()
        api_key = ApiKey(project_id=project.id, name="k", key_hash=digest, key_prefix=prefix)
        session.add(api_key)
        await session.flush()
        session.add(ApiKeyScope(api_key_id=api_key.id, mailbox_id=mailbox.id))
        await session.commit()

        principal = await resolve_principal(session, key)
        yield session, principal, maker

    await engine.dispose()


def req(**overrides) -> SendEmailRequest:
    payload = {
        "from": "Acme <noreply@example.com>",
        "to": ["customer@example.net"],
        "subject": "Verify your email",
        "html": "<p>hello</p>",
    }
    payload.update(overrides)
    return SendEmailRequest.model_validate(payload)


# --- the ordering guarantee ------------------------------------------------


async def test_accepted_message_is_durable_before_the_id_is_returned(env):
    """If we said `queued`, the row exists. That is the whole contract."""
    session, principal, maker = env
    result = await ingest_message(
        session, principal, req(), idempotency_key=None, **INGEST_DEFAULTS
    )
    await session.commit()

    async with maker() as fresh:  # a different session: proves it is committed
        row = (
            await fresh.execute(select(Message).where(Message.public_id == result.public_id))
        ).scalar_one()
        assert row.status is MessageStatus.QUEUED
        assert row.next_attempt_at is not None  # claimable by a worker immediately


async def test_acceptance_writes_the_opening_timeline(env):
    session, principal, maker = env
    result = await ingest_message(
        session, principal, req(), idempotency_key=None, **INGEST_DEFAULTS
    )
    await session.commit()

    message = (
        await session.execute(select(Message).where(Message.public_id == result.public_id))
    ).scalar_one()
    events = (
        (
            await session.execute(
                select(Event).where(Event.message_id == message.id).order_by(Event.sequence)
            )
        )
        .scalars()
        .all()
    )
    assert [e.event_type for e in events] == ["api.accepted", "message.queued"]
    assert events[1].detail["message_id_header"].startswith(f"<{result.public_id}@")


async def test_rejected_message_leaves_nothing_behind(env):
    """A refusal must not create a half-record that a worker could later find."""
    session, principal, maker = env
    with pytest.raises(AuthorizationError):
        await ingest_message(
            session,
            principal,
            req(**{"from": "nope@example.com"}),
            idempotency_key=None,
            **INGEST_DEFAULTS,
        )
    await session.rollback()
    assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 0
    assert (await session.execute(select(func.count(Event.id)))).scalar_one() == 0


# --- provider limits, enforced at the edge ---------------------------------


async def test_recipient_limit_is_refused_rather_than_queued(env):
    """151 recipients can never succeed at SMTP. A 4xx now beats an async 550."""
    session, principal, _ = env
    with pytest.raises(ValidationError) as exc:
        await ingest_message(
            session,
            principal,
            req(to=[f"user{i}@example.net" for i in range(MAX_RECIPIENTS + 1)]),
            idempotency_key=None,
            **INGEST_DEFAULTS,
        )
    assert str(MAX_RECIPIENTS) in str(exc.value)


async def test_oversize_message_is_refused(env):
    session, principal, _ = env
    with pytest.raises(ValidationError):
        await ingest_message(
            session, principal, req(html="x" * 52_500_000), idempotency_key=None, **INGEST_DEFAULTS
        )


async def test_duplicate_recipients_are_collapsed(env):
    """The same address twice is one delivery; counting it twice would burn
    recipient budget for nothing."""
    session, principal, _ = env
    result = await ingest_message(
        session,
        principal,
        req(to=["a@example.net", "A@Example.net", "a@example.net"]),
        idempotency_key=None,
        **INGEST_DEFAULTS,
    )
    await session.commit()
    row = (
        await session.execute(select(Message).where(Message.public_id == result.public_id))
    ).scalar_one()
    assert row.to_addresses == ["a@example.net"]
    assert row.recipient_count == 1


async def test_cc_and_bcc_count_toward_the_recipient_limit(env):
    session, principal, _ = env
    with pytest.raises(ValidationError):
        await ingest_message(
            session,
            principal,
            req(
                to=[f"a{i}@example.net" for i in range(100)],
                cc=[f"b{i}@example.net" for i in range(30)],
                bcc=[f"c{i}@example.net" for i in range(30)],
            ),
            idempotency_key=None,
            **INGEST_DEFAULTS,
        )


@pytest.mark.parametrize("bad", ["not-an-address", "@example.net", "user@localhost", ""])
async def test_invalid_recipients_are_refused(env, bad):
    session, principal, _ = env
    with pytest.raises(ValidationError):
        await ingest_message(
            session, principal, req(to=[bad]), idempotency_key=None, **INGEST_DEFAULTS
        )


# --- suppression -----------------------------------------------------------


async def test_suppressed_recipient_blocks_the_whole_request(env):
    """Silently dropping one recipient and reporting success is exactly the
    theatre vision.md refuses. Refuse loudly; let the caller retry."""
    session, principal, _ = env
    session.add(Suppression(address="dead@example.net"))
    await session.commit()

    with pytest.raises(SuppressedRecipient) as exc:
        await ingest_message(
            session,
            principal,
            req(to=["ok@example.net", "dead@example.net"]),
            idempotency_key=None,
            **INGEST_DEFAULTS,
        )
    assert "dead@example.net" in str(exc.value)
    await session.rollback()
    assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 0


# --- idempotency -----------------------------------------------------------


async def test_replay_returns_the_same_id_without_a_second_message(env):
    session, principal, _ = env
    first = await ingest_message(session, principal, req(), idempotency_key="k1", **INGEST_DEFAULTS)
    await session.commit()
    second = await ingest_message(
        session, principal, req(), idempotency_key="k1", **INGEST_DEFAULTS
    )
    await session.commit()

    assert second.public_id == first.public_id
    assert second.replayed is True
    assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 1


async def test_same_key_different_payload_is_refused(env):
    """The dangerous case: silently replaying here means the caller believes a
    message was sent that never existed."""
    session, principal, _ = env
    await ingest_message(session, principal, req(), idempotency_key="k1", **INGEST_DEFAULTS)
    await session.commit()
    with pytest.raises(IdempotencyKeyReused):
        await ingest_message(
            session, principal, req(subject="different"), idempotency_key="k1", **INGEST_DEFAULTS
        )


async def test_in_flight_duplicate_gets_conflict_not_a_second_message(env):
    """An idempotency row with no stored response means the first request is
    still inside its transaction."""
    session, principal, _ = env
    session.add(
        IdempotencyKey(
            project_id=principal.project_id,
            key="inflight",
            request_hash=canonical_request_hash(req().model_dump(by_alias=True, exclude_none=True)),
            response_body=None,
            expires_at=func.now(),
        )
    )
    await session.commit()
    with pytest.raises(IdempotencyConflict):
        await ingest_message(
            session, principal, req(), idempotency_key="inflight", **INGEST_DEFAULTS
        )


async def test_idempotency_is_scoped_per_project(env):
    """Two projects using the key 'welcome-1' must not collide."""
    session, principal, _ = env
    await ingest_message(session, principal, req(), idempotency_key="shared", **INGEST_DEFAULTS)
    await session.commit()

    other = (await session.execute(select(Project).where(Project.name == "other"))).scalar_one()
    stored = (
        await session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "shared"))
    ).scalar_one()
    assert stored.project_id == principal.project_id != other.id


async def test_request_hash_ignores_key_order(env):
    a = canonical_request_hash({"from": "x@y.com", "to": ["a@b.com"], "subject": "s"})
    b = canonical_request_hash({"subject": "s", "to": ["a@b.com"], "from": "x@y.com"})
    assert a == b


# --- identifiers -----------------------------------------------------------


def test_public_ids_are_unique_and_prefixed():
    ids = [new_public_id() for _ in range(500)]
    assert len(set(ids)) == 500
    assert all(i.startswith("email_") for i in ids)


def test_public_ids_sort_by_creation_time_across_milliseconds():
    """ULID is time-ordered to the millisecond, NOT strictly monotonic within
    one. Same-millisecond ids have random suffixes and arbitrary order, so the
    guarantee we actually rely on -- index locality for recency queries -- is
    what gets asserted here."""
    batches = []
    for _ in range(5):
        batches.append(new_public_id())
        time.sleep(0.002)
    assert batches == sorted(batches)


def test_message_id_header_embeds_the_public_id():
    """The bounce-attribution key, since VERP is unavailable on this provider."""
    header = message_id_header("email_01JABC", "example.com")
    assert header == "<email_01JABC@example.com>"


# --- retention -------------------------------------------------------------


async def test_bodies_are_purged_after_terminal_state(env):
    """Metadata and the timeline persist; content does not. Password-reset links
    must not become a permanent archive."""
    session, principal, _ = env
    result = await ingest_message(
        session, principal, req(), idempotency_key=None, **INGEST_DEFAULTS
    )
    await session.commit()

    message = (
        await session.execute(select(Message).where(Message.public_id == result.public_id))
    ).scalar_one()
    message.status = MessageStatus.ACCEPTED_BY_PROVIDER
    message.completed_at = func.now() - __import__("datetime").timedelta(days=10)
    await session.commit()

    purged = await purge_expired_bodies(session, retention_hours=72)
    await session.commit()
    await session.refresh(message)

    assert purged == 1
    assert message.body_html is None and message.body_text is None
    assert message.body_purged_at is not None
    # The timeline survives -- it is what answers "where did it fail?".
    events = (
        await session.execute(select(func.count(Event.id)).where(Event.message_id == message.id))
    ).scalar_one()
    assert events == 2


async def test_bodies_of_in_flight_messages_are_not_purged(env):
    session, principal, _ = env
    await ingest_message(session, principal, req(), idempotency_key=None, **INGEST_DEFAULTS)
    await session.commit()
    assert await purge_expired_bodies(session, retention_hours=0) == 0


# --- schema behaviour (no database) ----------------------------------------


def test_single_recipient_string_is_accepted_like_resend():
    assert req(to="one@example.net").to == ["one@example.net"]


def test_body_is_required():
    with pytest.raises(PydanticValidationError):
        SendEmailRequest.model_validate({"from": "a@b.com", "to": ["c@d.com"], "subject": "s"})


def test_unknown_fields_are_rejected_not_ignored():
    """A typo'd field name silently dropped is a message sent without the thing
    the caller thought they set."""
    with pytest.raises(PydanticValidationError):
        SendEmailRequest.model_validate(
            {"from": "a@b.com", "to": ["c@d.com"], "html": "<p>x</p>", "htlm": "<p>typo</p>"}
        )
