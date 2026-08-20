"""One project must not be able to enumerate another's suppressed addresses.

GET /v1/suppressions returned every entry on the installation. Suppressions are
mostly created by bounces, so the list is a register of real people's addresses
that failed -- one product's customers, readable by any other product's key.
The metrics endpoint's docstring claimed "scoped to the caller, like every
other read here", which was true of every read except this one.

Enforcement stays account-wide and these tests assert that too: reputation is
shared across every domain on the account, so an address that hard-bounced for
one product must stay blocked for all of them. Only *visibility* is scoped.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.models import Base, Project, SuppressionSource
from emaild.suppressions import (
    add_suppression,
    count_suppressions,
    is_suppressed,
    list_suppressions,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")


@pytest.fixture
async def session():
    engine = create_async_engine(TEST_DSN, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _two_projects(session) -> tuple[int, int]:
    a = Project(name="alpha", active=True)
    b = Project(name="beta", active=True)
    session.add_all([a, b])
    await session.flush()
    return a.id, b.id


async def test_a_project_sees_only_its_own(session):
    alpha, beta = await _two_projects(session)
    await add_suppression(session, "dead@alpha-customer.com", project_id=alpha)
    await add_suppression(session, "dead@beta-customer.com", project_id=beta)

    seen = {s.address for s in await list_suppressions(session, project_id=alpha)}
    assert seen == {"dead@alpha-customer.com"}
    assert "dead@beta-customer.com" not in seen


async def test_the_count_is_scoped_too(session):
    """A scoped list beside a global count still leaks the global total."""
    alpha, beta = await _two_projects(session)
    await add_suppression(session, "a@x.com", project_id=alpha)
    await add_suppression(session, "b@x.com", project_id=beta)
    await add_suppression(session, "c@x.com", project_id=beta)

    assert await count_suppressions(session, project_id=alpha) == 1
    assert await count_suppressions(session, project_id=beta) == 2
    assert await count_suppressions(session) == 3  # operator view


async def test_the_operator_still_sees_everything(session):
    alpha, beta = await _two_projects(session)
    await add_suppression(session, "a@x.com", project_id=alpha)
    await add_suppression(session, "b@x.com", project_id=beta)
    await add_suppression(session, "operator@x.com")  # no project

    seen = {s.address for s in await list_suppressions(session)}
    assert seen == {"a@x.com", "b@x.com", "operator@x.com"}


async def test_unattributed_entries_are_invisible_to_every_project(session):
    """Operator-added, and anything predating the column. Withholding is the
    conservative reading -- we cannot know whose customer it was."""
    alpha, _ = await _two_projects(session)
    await add_suppression(session, "mystery@x.com")

    assert [s.address for s in await list_suppressions(session, project_id=alpha)] == []
    assert len(await list_suppressions(session)) == 1


async def test_blocking_remains_account_wide(session):
    """The point of the design. Visibility is scoped; enforcement is not --
    reputation is shared across every domain on the account."""
    alpha, beta = await _two_projects(session)
    await add_suppression(
        session, "dead@x.com", source=SuppressionSource.BOUNCE, project_id=alpha
    )

    # beta cannot SEE it...
    assert [s.address for s in await list_suppressions(session, project_id=beta)] == []
    # ...but is still blocked from mailing it.
    assert await is_suppressed(session, "dead@x.com")


async def test_attribution_does_not_change_idempotency(session):
    """Re-suppressing an address already suppressed by another project is still
    a no-op, and must not silently reassign it."""
    alpha, beta = await _two_projects(session)
    first, created_first = await add_suppression(session, "shared@x.com", project_id=alpha)
    second, created_second = await add_suppression(session, "shared@x.com", project_id=beta)

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.project_id == alpha
