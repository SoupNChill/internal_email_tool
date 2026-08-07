"""First-run bootstrap.

Currently one job: make sure this installation has an identity.

Required by first_production_packaging.md §10 — the id must exist, persist
across upgrades, survive backup and restore, and appear in the backup manifest so
a restored archive can be matched to where it came from.

It is generated here rather than by a migration deliberately. A migration runs
once per *schema*, but this must be idempotent per *installation*: restoring a
backup into a fresh database must keep the original id, not mint a new one, and a
migration-time insert would fight that.
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from emaild import __version__
from emaild.models import Installation

log = logging.getLogger(__name__)

INSTALLATION_ID_PREFIX = "inst_"


def new_installation_id() -> str:
    """Random, and carrying nothing.

    §10 requires the identifier to avoid customer names, IP addresses,
    hostnames, and personal information. Pure CSPRNG output satisfies that by
    construction -- unlike, say, a ULID, whose timestamp would leak install date
    into every backup manifest and support bundle for no benefit.
    """
    return f"{INSTALLATION_ID_PREFIX}{secrets.token_hex(12)}"


async def ensure_installation(session: AsyncSession) -> str:
    """Return this installation's id, creating it on first run.

    Idempotent and race-safe: `ON CONFLICT DO NOTHING` against the single-row
    check constraint means several processes starting at once cannot produce two
    identities, and a restart cannot replace one.
    """
    existing = (await session.execute(select(Installation.installation_id))).scalar_one_or_none()
    if existing is not None:
        return existing

    candidate = new_installation_id()
    await session.execute(
        pg_insert(Installation)
        .values(id=1, installation_id=candidate, installed_version=__version__)
        .on_conflict_do_nothing(index_elements=[Installation.id])
    )

    # Re-read rather than trusting the insert: if another process won the race,
    # the row that exists is theirs, and that is the one to report.
    settled = (await session.execute(select(Installation.installation_id))).scalar_one()
    if settled == candidate:
        log.info("installation identity created: %s", settled)
    return settled


async def get_installation(session: AsyncSession) -> Installation | None:
    return (await session.execute(select(Installation))).scalar_one_or_none()
