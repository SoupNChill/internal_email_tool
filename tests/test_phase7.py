"""Phase 7 observability tests.

Metrics that are subtly wrong are worse than no metrics: they produce confident
answers nobody checks. So these assert exact numbers, and the project-scoping
tests treat cross-tenant disclosure as the failure it is.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.metrics import (
    HEARTBEAT_STALE_AFTER,
    QUEUE_AGE_WARNING,
    build_overview,
    queue_health,
    rate_headroom,
    worker_status,
)
from emaild.models import (
    Base,
    Domain,
    DomainStatus,
    FailureClass,
    Mailbox,
    Message,
    MessageStatus,
    Project,
    WorkerHeartbeat,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")


@pytest.fixture
async def env():
    engine = create_async_engine(TEST_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        billing = Project(name="billing")
        shipping = Project(name="shipping")
        d1 = Domain(name="one.com", status=DomainStatus.READY)
        d2 = Domain(name="two.com", status=DomainStatus.READY)
        session.add_all([billing, shipping, d1, d2])
        await session.flush()
        m1 = Mailbox(domain_id=d1.id, address="a@one.com", password_encrypted="x", hourly_limit=10)
        m2 = Mailbox(domain_id=d2.id, address="b@two.com", password_encrypted="x", hourly_limit=10)
        session.add_all([m1, m2])
        await session.commit()
        yield session, billing, shipping, m1, m2
    await engine.dispose()


_seq = 0


async def _msg(session, project, mailbox, status, **kw):
    global _seq
    _seq += 1
    m = Message(
        public_id=f"email_{_seq:08d}",
        project_id=project.id,
        mailbox_id=mailbox.id,
        from_address=mailbox.address,
        to_addresses=["x@example.net"],
        recipient_count=1,
        status=status,
        **kw,
    )
    session.add(m)
    await session.commit()
    return m


# --- queue health ----------------------------------------------------------


async def test_empty_queue_is_healthy(env):
    session, *_ = env
    q = await queue_health(session)
    assert q.healthy is True and q.pending == 0 and q.oldest_pending_seconds is None


async def test_fresh_pending_work_is_healthy(env):
    session, billing, _, m1, _ = env
    await _msg(session, billing, m1, MessageStatus.QUEUED)
    q = await queue_health(session)
    assert q.pending == 1 and q.healthy is True


async def test_old_pending_work_is_the_signal_that_something_is_wrong(env):
    """One number that catches a dead worker, a stuck rate gate, and a provider
    outage alike."""
    session, billing, _, m1, _ = env
    old = datetime.now(UTC) - QUEUE_AGE_WARNING - timedelta(minutes=5)
    await _msg(session, billing, m1, MessageStatus.QUEUED, created_at=old)
    q = await queue_health(session)
    assert q.healthy is False
    assert "minutes old" in (q.reason or "")


async def test_age_is_measured_from_creation_not_from_next_attempt(env):
    """A message deferred with a long backoff is still one the caller is waiting
    on. Measuring from next_attempt_at would give the reassuring answer."""
    session, billing, _, m1, _ = env
    old = datetime.now(UTC) - timedelta(hours=2)
    await _msg(
        session,
        billing,
        m1,
        MessageStatus.TEMPORARILY_FAILED,
        created_at=old,
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )
    q = await queue_health(session)
    assert q.oldest_pending_seconds > 7000
    assert q.healthy is False


async def test_needs_review_makes_the_queue_unhealthy(env):
    session, billing, _, m1, _ = env
    await _msg(session, billing, m1, MessageStatus.PERMANENTLY_REJECTED, needs_review=True)
    q = await queue_health(session)
    assert q.needs_review == 1 and q.healthy is False


# --- volume and failure rate ----------------------------------------------


async def test_totals_and_failure_rate_are_exact(env):
    session, billing, _, m1, _ = env
    for _ in range(7):
        await _msg(session, billing, m1, MessageStatus.ACCEPTED_BY_PROVIDER)
    for _ in range(3):
        await _msg(
            session,
            billing,
            m1,
            MessageStatus.PERMANENTLY_REJECTED,
            failure_class=FailureClass.RECIPIENT_REJECTED,
        )
    await _msg(session, billing, m1, MessageStatus.QUEUED)

    o = await build_overview(session)
    assert o.requested == 11
    assert o.accepted == 7 and o.failed == 3 and o.pending == 1
    # Rate is over SETTLED messages: counting pending ones would understate it
    # early and drift as they resolve.
    assert o.failure_rate == 0.3


async def test_messages_outside_the_window_are_excluded(env):
    session, billing, _, m1, _ = env
    await _msg(
        session,
        billing,
        m1,
        MessageStatus.ACCEPTED_BY_PROVIDER,
        created_at=datetime.now(UTC) - timedelta(days=3),
    )
    await _msg(session, billing, m1, MessageStatus.ACCEPTED_BY_PROVIDER)
    assert (await build_overview(session, window_hours=24)).requested == 1
    assert (await build_overview(session, window_hours=24 * 7)).requested == 2


async def test_breakdown_by_domain_and_project(env):
    session, billing, shipping, m1, m2 = env
    await _msg(session, billing, m1, MessageStatus.ACCEPTED_BY_PROVIDER)
    await _msg(session, billing, m1, MessageStatus.ACCEPTED_BY_PROVIDER)
    await _msg(session, shipping, m2, MessageStatus.ACCEPTED_BY_PROVIDER)

    o = await build_overview(session)
    by_domain = {s.name: s.requested for s in o.by_domain}
    by_project = {s.name: s.requested for s in o.by_project}
    assert by_domain == {"one.com": 2, "two.com": 1}
    assert by_project == {"billing": 2, "shipping": 1}


async def test_failures_are_grouped_by_class(env):
    session, billing, _, m1, _ = env
    await _msg(
        session,
        billing,
        m1,
        MessageStatus.PERMANENTLY_REJECTED,
        failure_class=FailureClass.RECIPIENT_REJECTED,
    )
    await _msg(
        session,
        billing,
        m1,
        MessageStatus.PERMANENTLY_REJECTED,
        failure_class=FailureClass.RECIPIENT_REJECTED,
    )
    await _msg(
        session,
        billing,
        m1,
        MessageStatus.TEMPORARILY_FAILED,
        failure_class=FailureClass.PROVIDER_DEFERRAL,
    )
    o = await build_overview(session)
    assert o.failures_by_class == {"recipient_rejected": 2, "provider_deferral": 1}


# --- project scoping -------------------------------------------------------


async def test_metrics_scoped_to_a_project_exclude_other_projects(env):
    """Cross-tenant disclosure: "how much mail does that team send?" is not a
    question one API key should answer about another."""
    session, billing, shipping, m1, m2 = env
    for _ in range(5):
        await _msg(session, billing, m1, MessageStatus.ACCEPTED_BY_PROVIDER)
    for _ in range(9):
        await _msg(session, shipping, m2, MessageStatus.ACCEPTED_BY_PROVIDER)

    scoped = await build_overview(session, project_id=billing.id)
    assert scoped.requested == 5
    assert {s.name for s in scoped.by_domain} == {"one.com"}

    unscoped = await build_overview(session)
    assert unscoped.requested == 14


# --- latency ---------------------------------------------------------------


async def test_latency_percentiles_are_not_dragged_by_one_outlier(env):
    """A mean would be. One 30-second timeout must not make p50 describe a
    request that never happened."""
    session, billing, _, m1, _ = env
    for value in [100] * 19 + [30_000]:
        await _msg(
            session, billing, m1, MessageStatus.ACCEPTED_BY_PROVIDER, provider_latency_ms=value
        )
    lat = (await build_overview(session)).latency_ms
    assert lat["p50"] == 100
    assert lat["max"] == 30_000
    assert lat["samples"] == 20


async def test_latency_is_absent_when_nothing_has_been_sent(env):
    session, *_ = env
    lat = (await build_overview(session)).latency_ms
    assert lat["p50"] is None and lat["samples"] == 0


# --- workers ---------------------------------------------------------------


async def test_fresh_heartbeat_reads_as_alive(env):
    session, *_ = env
    session.add(WorkerHeartbeat(worker_id="w1", version="0.1.0", messages_processed=3))
    await session.commit()
    status = await worker_status(session)
    assert len(status) == 1 and status[0]["alive"] is True


async def test_stale_heartbeat_reads_as_dead(env):
    session, *_ = env
    session.add(
        WorkerHeartbeat(
            worker_id="w1",
            last_seen_at=datetime.now(UTC) - HEARTBEAT_STALE_AFTER - timedelta(minutes=1),
        )
    )
    await session.commit()
    assert (await worker_status(session))[0]["alive"] is False


async def test_no_workers_is_reported_rather_than_assumed_healthy(env):
    session, *_ = env
    assert await worker_status(session) == []


# --- rate headroom ---------------------------------------------------------


async def test_headroom_shows_distance_to_our_gate_not_the_provider_wall(env):
    session, billing, _, m1, _ = env
    for _ in range(5):
        await _msg(
            session,
            billing,
            m1,
            MessageStatus.ACCEPTED_BY_PROVIDER,
            last_attempt_at=datetime.now(UTC),
        )
    rows = {r["sender"]: r for r in await rate_headroom(session, 0.9, None)}
    row = rows["a@one.com"]
    assert row["used_this_hour"] == 5
    assert row["our_ceiling"] == 9  # 90% of 10
    assert row["provider_ceiling"] == 10
    assert row["remaining"] == 4


async def test_headroom_ignores_attempts_outside_the_rolling_hour(env):
    session, billing, _, m1, _ = env
    await _msg(
        session,
        billing,
        m1,
        MessageStatus.ACCEPTED_BY_PROVIDER,
        last_attempt_at=datetime.now(UTC) - timedelta(hours=2),
    )
    rows = {r["sender"]: r for r in await rate_headroom(session, 0.9, None)}
    assert rows["a@one.com"]["used_this_hour"] == 0
