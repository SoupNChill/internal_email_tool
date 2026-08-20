"""Project and API-key operations, shared by the CLI and the dashboard.

These moved out of emaild/admin.py when the dashboard gained the ability to
perform them. Two copies of "create a scoped API key" would eventually disagree
about something that matters -- which scopes are required, whether an unknown
mailbox is an error -- and the copy with the weaker rule would be the one
reachable from a browser.

WHY THESE AND NOT THE REST. Everything here touches only the database. Domain
and mailbox operations need the MXRoute account-root credential and the mailbox
encryption key, and role=api is forbidden both (emaild/config.py), so the
dashboard could not perform them even if it wanted to. That split is not a
policy decision that might be revisited under pressure -- it is enforced by
which Docker volume each container mounts.

Every function here raises ManagementError with a message written for a human,
because both callers display it directly: one to a terminal, one in a browser.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.crypto import generate_api_key
from emaild.models import ApiKey, ApiKeyScope, Mailbox, Project

log = logging.getLogger(__name__)

# Long enough to be meaningful in a log line, short enough to fit a column.
MAX_NAME_LENGTH = 64


class ManagementError(Exception):
    """A request that cannot be satisfied, explained for whoever asked."""


def _clean_name(value: str, *, what: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ManagementError(f"A {what} name is required.")
    if len(name) > MAX_NAME_LENGTH:
        raise ManagementError(f"That {what} name is too long (max {MAX_NAME_LENGTH} characters).")
    return name


async def create_project(
    session: AsyncSession,
    name: str,
    description: str | None = None,
    *,
    actor: str = "cli",
) -> Project:
    """Create a sending project. Names are unique."""
    name = _clean_name(name, what="project")

    existing = (
        await session.execute(select(Project).where(Project.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        raise ManagementError(f"A project named '{name}' already exists.")

    project = Project(name=name, description=(description or "").strip() or None)
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Lost a race with another creator. Same outcome the check above would
        # have produced, so report it the same way.
        await session.rollback()
        raise ManagementError(f"A project named '{name}' already exists.") from exc

    log.info("project created: %s (by %s)", name, actor)
    return project


async def create_api_key(
    session: AsyncSession,
    name: str,
    project_name: str,
    mailbox_addresses: list[str],
    *,
    actor: str = "cli",
) -> tuple[ApiKey, str, list[str]]:
    """Create a scoped key. Returns (key, plaintext, scoped addresses).

    The plaintext is returned once and never stored -- only a SHA-256 digest
    is. Callers must show it immediately or lose it.
    """
    name = _clean_name(name, what="key")

    project = (
        await session.execute(select(Project).where(Project.name == project_name))
    ).scalar_one_or_none()
    if project is None:
        raise ManagementError(f"No such project: {project_name}")
    if not project.active:
        raise ManagementError(
            f"Project '{project_name}' is inactive, so a key for it could not authenticate."
        )

    wanted = sorted({a.strip().lower() for a in mailbox_addresses if a and a.strip()})
    if not wanted:
        # A key with no scopes authenticates and can send as nothing, which
        # looks like a broken key rather than an empty one. Refuse at creation
        # rather than at the first confusing send.
        raise ManagementError(
            "Select at least one sender identity. A key with no scopes cannot send as anything."
        )

    mailboxes = (
        (await session.execute(select(Mailbox).where(Mailbox.address.in_(wanted)))).scalars().all()
    )
    found = {m.address for m in mailboxes}
    if missing := sorted(set(wanted) - found):
        raise ManagementError(f"Unknown sender identities: {', '.join(missing)}")

    duplicate = (
        await session.execute(select(ApiKey).where(ApiKey.name == name))
    ).scalar_one_or_none()
    if duplicate is not None and duplicate.revoked_at is None:
        # Revoked names are reusable -- rotating a key by revoking and
        # recreating under the same name is the documented procedure, and this
        # is exactly what the operator did on the first production install.
        raise ManagementError(f"An active key named '{name}' already exists.")

    full_key, key_hash, prefix = generate_api_key()
    api_key = ApiKey(project_id=project.id, name=name, key_hash=key_hash, key_prefix=prefix)
    session.add(api_key)
    await session.flush()
    for mailbox in mailboxes:
        session.add(ApiKeyScope(api_key_id=api_key.id, mailbox_id=mailbox.id))
    await session.flush()

    log.info(
        "api key created: %s for project %s scoped to %s (by %s)",
        name,
        project_name,
        ", ".join(sorted(found)),
        actor,
    )
    return api_key, full_key, sorted(found)


async def revoke_api_key(session: AsyncSession, name: str, *, actor: str = "cli") -> str:
    """Revoke a key. Returns a message describing what happened.

    Idempotent: revoking an already-revoked key is not an error, because the
    caller's intent is already satisfied.
    """
    key = (await session.execute(select(ApiKey).where(ApiKey.name == name))).scalar_one_or_none()
    if key is None:
        raise ManagementError(f"No such key: {name}")

    if key.revoked_at is not None:
        return f"Key '{name}' was already revoked at {key.revoked_at:%Y-%m-%d %H:%M}."

    key.revoked_at = datetime.now(UTC)
    key.active = False
    await session.flush()

    log.warning("api key revoked: %s (by %s)", name, actor)
    return f"Revoked '{name}'. Effective immediately -- authentication is never cached."
