"""Authentication and authorization.

Answers the four questions vision.md requires of every request:

    1. Who is making this request?              -> Principal (key + project)
    2. Which domain are they allowed to use?    -> via the key's scoped mailboxes
    3. Which sender identities are permitted?   -> those mailboxes, exactly
    4. Is the request within that authorization? -> authorize_sender()

Two deliberate non-decisions worth stating, because both are tempting:

**Nothing here is cached.** A key lookup is one indexed query, and at this volume
that is cheap. Caching would buy microseconds and cost immediate revocation --
and a revoked key that keeps working for another 60 seconds is exactly the
failure a revocation feature exists to prevent.

**Lookup is by hash, not by comparison.** We hash the presented key and query for
that digest. No secret is ever compared in our code, so there is no timing signal
to leak in the first place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from emaild.crypto import API_KEY_PREFIX, hash_api_key
from emaild.db import session_scope
from emaild.errors import AuthenticationError, AuthorizationError, DomainNotReady
from emaild.models import ApiKey, Domain, DomainStatus, Mailbox, Project

log = logging.getLogger(__name__)

# How stale `last_used_at` may become before we spend a write on it. Exact
# to-the-second accuracy is worth less than avoiding a write on every request;
# "was this key used today?" is the question it actually answers.
LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass
class Principal:
    """An authenticated caller and everything it is allowed to do."""

    api_key_id: int
    key_name: str
    project_id: int
    project_name: str
    mailboxes: dict[str, Mailbox]  # address -> mailbox, the permitted senders

    @property
    def allowed_addresses(self) -> list[str]:
        return sorted(self.mailboxes)

    @property
    def allowed_domains(self) -> set[str]:
        return {a.split("@", 1)[1] for a in self.mailboxes}


def _extract_bearer(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header:
        raise AuthenticationError(
            "Missing Authorization header. Send: Authorization: Bearer em_live_..."
        )

    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError(
            "Malformed Authorization header. Expected: Authorization: Bearer em_live_..."
        )

    token = token.strip()
    if not token.startswith(API_KEY_PREFIX):
        # Cheap shape check before touching the database. Says what a key looks
        # like without echoing what was sent -- a mistyped header often contains
        # a real secret from somewhere else.
        raise AuthenticationError(f"Invalid API key format. Keys begin with '{API_KEY_PREFIX}'.")
    return token


async def _touch_last_used(session: AsyncSession, api_key: ApiKey) -> None:
    now = datetime.now(UTC)
    previous = api_key.last_used_at
    if previous is None or (now - previous) > LAST_USED_RESOLUTION:
        api_key.last_used_at = now


async def resolve_principal(session: AsyncSession, presented_key: str) -> Principal:
    """Authenticate a key and load its authorization. Raises AuthenticationError."""
    digest = hash_api_key(presented_key)

    api_key = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.key_hash == digest)
            .options(selectinload(ApiKey.scopes), selectinload(ApiKey.project))
        )
    ).scalar_one_or_none()

    # Unknown, revoked, and deactivated all produce one message. A caller with a
    # legitimate key needs the same action in every case; anyone probing learns
    # nothing about which keys exist.
    if api_key is None:
        log.warning("auth: unknown API key presented (prefix %s)", presented_key[:14])
        raise AuthenticationError("Invalid API key.")
    if api_key.revoked_at is not None or not api_key.active:
        log.warning("auth: revoked or inactive key used: %s", api_key.name)
        raise AuthenticationError("Invalid API key.")

    project: Project = api_key.project
    if not project.active:
        log.warning("auth: key %s belongs to inactive project %s", api_key.name, project.name)
        raise AuthenticationError("Invalid API key.")

    mailbox_ids = [s.mailbox_id for s in api_key.scopes]
    mailboxes: dict[str, Mailbox] = {}
    if mailbox_ids:
        rows = (
            (
                await session.execute(
                    select(Mailbox)
                    .where(Mailbox.id.in_(mailbox_ids))
                    .options(selectinload(Mailbox.domain))
                )
            )
            .scalars()
            .all()
        )
        mailboxes = {m.address: m for m in rows}

    await _touch_last_used(session, api_key)

    return Principal(
        api_key_id=api_key.id,
        key_name=api_key.name,
        project_id=project.id,
        project_name=project.name,
        mailboxes=mailboxes,
    )


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: authenticate the request or refuse it."""
    token = _extract_bearer(request)
    async with session_scope() as session:
        principal = await resolve_principal(session, token)
    return principal


CurrentPrincipal = Depends(require_principal)


def parse_from_address(value: str) -> tuple[str | None, str]:
    """Split `Acme <noreply@x.com>` into (display_name, address).

    Resend-compatible input. The display name is free-form; the address is not --
    MXRoute pins the SMTP envelope sender to the authenticated login, so it must
    match a provisioned mailbox exactly.
    """
    display_name, address = parseaddr(value or "")
    address = address.strip().lower()

    # `parseaddr` is lenient by design: it happily returns "@example.com" (no
    # local part) or "user@host" (no TLD). Both would be accepted here and
    # rejected later by SMTP, turning an obvious input error into a queued
    # message that can never succeed. Validate the shape properly instead.
    local, _, domain_part = address.rpartition("@")
    if not local or not domain_part or "." not in domain_part or domain_part.startswith("."):
        raise AuthorizationError(
            f"Could not parse a valid sender address from {value!r}. "
            "Use 'noreply@example.com' or 'Acme <noreply@example.com>'.",
            param="from",
        )
    return (display_name.strip() or None), address


def authorize_sender(principal: Principal, from_value: str) -> tuple[str | None, Mailbox]:
    """Question 4: may this caller send as this identity, right now?

    Returns (display_name, mailbox). Raises AuthorizationError or DomainNotReady.
    """
    display_name, address = parse_from_address(from_value)

    mailbox = principal.mailboxes.get(address)
    if mailbox is None:
        if not principal.mailboxes:
            raise AuthorizationError(
                f"This API key has no sender identities. Grant one with: "
                f"keys create ... --mailbox {address}",
                param="from",
            )
        # The key is authenticated, so naming what it *can* send as turns this
        # from a guessing game into a one-line fix.
        raise AuthorizationError(
            f"This API key is not permitted to send as {address}. "
            f"Permitted: {', '.join(principal.allowed_addresses)}.",
            param="from",
        )

    if not mailbox.active:
        raise AuthorizationError(f"Sender identity {address} is deactivated.", param="from")

    domain: Domain = mailbox.domain
    if domain.status is not DomainStatus.READY:
        # A different failure with a different fix: publish DNS, do not widen the
        # key. Saying which checks failed saves a round trip to the dashboard.
        failing = ""
        if isinstance(domain.dns_state, dict):
            checks = domain.dns_state.get("checks") or {}
            bad = [k for k, v in checks.items() if v.get("result") != "pass" and k != "dmarc"]
            if bad:
                failing = f" Failing checks: {', '.join(sorted(bad))}."

        # Naming the status without naming the remedy sends the caller to the
        # docs to translate one into the other. 'verified' especially: its DNS
        # is already correct and the fix is a single command, which reads
        # nothing like a DNS problem.
        remedy = {
            DomainStatus.VERIFIED: (
                " Its DNS is complete; run 'appctl admin domains verify"
                f" {domain.name}' to promote it to ready."
            ),
            DomainStatus.ADDED: (
                " Publish its DNS records ('appctl admin domains records"
                f" {domain.name}'), then verify it."
            ),
            DomainStatus.OWNERSHIP_PENDING: (" The ownership TXT record is not resolving yet."),
            DomainStatus.DNS_INCOMPLETE: (
                " Publish the missing records, then run 'appctl admin domains"
                f" verify {domain.name}'."
            ),
            DomainStatus.MISCONFIGURED: (
                " Its DNS used to pass and no longer does -- something changed" " outside emaild."
            ),
            DomainStatus.SUSPENDED: " An operator suspended it.",
        }.get(domain.status, "")

        raise DomainNotReady(
            f"Domain {domain.name} is '{domain.status.value}' and cannot send."
            f"{failing}{remedy}",
            param="from",
        )

    return display_name, mailbox
