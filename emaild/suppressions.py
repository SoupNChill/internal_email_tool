"""Suppression list -- addresses we refuse to send to.

This is the only brake that exists. Bad external recipients come back `250
Accepted` and bounce out of band (spike_results.md, Finding 2), so nothing else
stops us mailing a dead address forever.

Two properties matter more than they look:

**It cannot be reconstructed.** Unlike domain state, which rebuilds from the
provider API and DNS, a suppression list is accumulated knowledge. Losing it
means re-learning every entry the expensive way. It is one of only two things in
this system with no recovery path (deployment_and_release.md §4).

**Removal is more dangerous than addition.** Adding a suppression fails closed --
we stop mailing someone. Removing one resumes mail to an address we previously
had reason to distrust. The two directions therefore have different permissions:
any API key may add, only an operator may remove.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.utils import parseaddr

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.models import Suppression, SuppressionSource

log = logging.getLogger(__name__)


class InvalidAddress(ValueError):
    pass


def normalise_address(raw: str) -> str:
    """Canonical form for storage and comparison.

    Must match how ingest normalises recipients exactly, or a suppressed address
    written one way silently fails to block the same address written another.
    Case-folded, display name stripped.

    Deliberately does NOT strip plus-addressing: `a+tag@x.com` and `a@x.com` are
    different mailboxes as far as any receiving server is concerned, and folding
    them would suppress mail the operator never asked to stop.
    """
    _, address = parseaddr(raw or "")
    address = address.strip().lower()
    local, _, domain = address.rpartition("@")
    if not local or not domain or "." not in domain or domain.startswith("."):
        raise InvalidAddress(f"not a valid email address: {raw!r}")
    return address


@dataclass
class SuppressionRecord:
    address: str
    source: str
    reason: str | None
    created_at: str


async def is_suppressed(session: AsyncSession, address: str) -> bool:
    normalised = normalise_address(address)
    return (
        await session.execute(
            select(func.count(Suppression.id)).where(Suppression.address == normalised)
        )
    ).scalar_one() > 0


async def find_suppressed(session: AsyncSession, addresses: list[str]) -> set[str]:
    """Which of these are suppressed. One query, not N."""
    normalised = []
    for raw in addresses:
        try:
            normalised.append(normalise_address(raw))
        except InvalidAddress:
            continue
    if not normalised:
        return set()
    rows = (
        (
            await session.execute(
                select(Suppression.address).where(Suppression.address.in_(normalised))
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def add_suppression(
    session: AsyncSession,
    address: str,
    *,
    source: SuppressionSource = SuppressionSource.MANUAL,
    reason: str | None = None,
    project_id: int | None = None,
) -> tuple[Suppression, bool]:
    """Suppress an address. Returns (record, created).

    Idempotent: suppressing an already-suppressed address is a no-op rather than
    an error, because the caller's intent is already satisfied and failing would
    make retry logic pointlessly awkward.
    """
    normalised = normalise_address(address)

    existing = (
        await session.execute(select(Suppression).where(Suppression.address == normalised))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    record = Suppression(address=normalised, source=source, reason=reason, project_id=project_id)
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        # Another worker suppressed it between our SELECT and INSERT. Same
        # outcome either way.
        await session.rollback()
        existing = (
            await session.execute(select(Suppression).where(Suppression.address == normalised))
        ).scalar_one()
        return existing, False

    log.info("suppressed %s (source=%s)", normalised, source.value)
    return record, True


async def remove_suppression(session: AsyncSession, address: str) -> bool:
    """Un-suppress. Operator-only: this resumes mail to a distrusted address."""
    normalised = normalise_address(address)
    result = await session.execute(delete(Suppression).where(Suppression.address == normalised))
    removed = bool(result.rowcount)
    if removed:
        log.warning("suppression REMOVED for %s -- sending to it will resume", normalised)
    return removed


async def list_suppressions(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    project_id: int | None = None,
) -> list[Suppression]:
    """Suppressed addresses, optionally narrowed to one project.

    `project_id=None` means the OPERATOR view -- everything. Callers reachable
    by an API key must always pass one: without it, this endpoint let any key
    enumerate every suppressed address on the installation, including other
    products' bounced customers. See emaild/api/v1.py.
    """
    stmt = select(Suppression).order_by(Suppression.created_at.desc())
    if project_id is not None:
        stmt = stmt.where(Suppression.project_id == project_id)
    return list(
        (await session.execute(stmt.limit(min(limit, 1000)).offset(offset))).scalars().all()
    )


async def count_suppressions(session: AsyncSession, *, project_id: int | None = None) -> int:
    """Total. Must be scoped the same way as the listing it accompanies --
    a scoped list beside a global count still discloses the global total."""
    stmt = select(func.count(Suppression.id))
    if project_id is not None:
        stmt = stmt.where(Suppression.project_id == project_id)
    return (await session.execute(stmt)).scalar_one()
