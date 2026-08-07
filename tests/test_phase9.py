"""Phase 9 tests: installation identity.

Backup and restore are exercised as a drill against a live stack rather than
here — §24 requires restore onto a *clean machine*, and a test that stubs the
database would prove nothing about the thing that matters.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.bootstrap import (
    INSTALLATION_ID_PREFIX,
    ensure_installation,
    get_installation,
    new_installation_id,
)
from emaild.models import Base, Installation

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")


@pytest.fixture
async def maker():
    engine = create_async_engine(TEST_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# --- identifier shape (pure) -----------------------------------------------


def test_identifier_carries_no_information():
    """§10 forbids customer names, IPs, hostnames, and personal data. Random
    output satisfies that by construction -- unlike a ULID, whose timestamp
    would leak the install date into every manifest and support bundle."""
    ids = [new_installation_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(i.startswith(INSTALLATION_ID_PREFIX) for i in ids)
    # No embedded ordering: random ids must not sort by creation.
    assert ids != sorted(ids)


# --- generation ------------------------------------------------------------


async def test_created_on_first_run(maker):
    async with maker() as session:
        created = await ensure_installation(session)
        await session.commit()
    assert created.startswith(INSTALLATION_ID_PREFIX)

    async with maker() as session:
        row = await get_installation(session)
    assert row is not None and row.installation_id == created


async def test_second_call_returns_the_same_identity(maker):
    """Restart must not mint a new identity."""
    async with maker() as session:
        first = await ensure_installation(session)
        await session.commit()
    async with maker() as session:
        second = await ensure_installation(session)
        await session.commit()
    assert first == second


async def test_concurrent_startup_cannot_create_two(maker):
    """API and worker start together. Two identities would make every backup
    manifest ambiguous about where it came from."""

    async def start() -> str:
        async with maker() as session:
            value = await ensure_installation(session)
            await session.commit()
            return value

    results = await asyncio.gather(*(start() for _ in range(5)), return_exceptions=True)
    settled = [r for r in results if isinstance(r, str)]
    assert settled, f"every concurrent start failed: {results}"
    assert len(set(settled)) == 1, f"race produced multiple identities: {set(settled)}"

    async with maker() as session:
        rows = (await session.execute(select(Installation))).scalars().all()
    assert len(rows) == 1


async def test_records_the_version_it_was_installed_on(maker):
    from emaild import __version__

    async with maker() as session:
        await ensure_installation(session)
        await session.commit()
        row = await get_installation(session)
    assert row is not None and row.installed_version == __version__


async def test_identity_is_preserved_not_replaced(maker):
    """A restored backup keeps the ORIGINAL id -- the whole point of putting it
    in the manifest. Simulated here by pre-seeding a row as pg_restore would."""
    async with maker() as session:
        session.add(
            Installation(id=1, installation_id="inst_fromabackup", installed_version="0.1.0")
        )
        await session.commit()

    async with maker() as session:
        value = await ensure_installation(session)
        await session.commit()
    assert value == "inst_fromabackup"

    async with maker() as session:
        row = await get_installation(session)
    # The version stays as it was recorded at original install, not overwritten.
    assert row is not None and row.installed_version == "0.1.0"


async def test_single_row_constraint_is_enforced_by_the_database(maker):
    async with maker() as session:
        await ensure_installation(session)
        await session.commit()

    async with maker() as session:
        session.add(Installation(id=2, installation_id="inst_second", installed_version="x"))
        with pytest.raises(Exception):  # noqa: B017 - driver-specific IntegrityError
            await session.commit()
