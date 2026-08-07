"""Mailbox provisioning -- creating sender identities.

The governing constraint (spike_results.md, Finding 1): MXRoute pins the SMTP
envelope sender to the authenticated login *exactly*. A mailbox and a sender
identity are therefore one-to-one, and no application-level design routes around
it.

This also sets the boundary against MXRoute's acceptable-use policy. Distinct
real addresses are legitimate; minting extra mailboxes for one identity to
multiply the 400/hour budget is prohibited and carries account termination. The
difference is intent, so this module refuses the second case explicitly rather
than leaving it to whoever is holding the CLI at the time.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.crypto import MailboxCipher, generate_mailbox_password
from emaild.models import Domain, DomainStatus, Mailbox
from emaild.providers.mxroute import MXRouteClient, MXRouteConflict

log = logging.getLogger(__name__)

# MXRoute's per-mailbox ceilings. Not ours to raise.
PROVIDER_HOURLY_LIMIT = 400
PROVIDER_DAILY_LIMIT = 9600


class ProvisioningError(Exception):
    pass


class PolicyViolation(ProvisioningError):
    """The requested action would breach MXRoute's acceptable-use policy."""


async def provision_mailbox(
    session: AsyncSession,
    client: MXRouteClient,
    cipher: MailboxCipher,
    *,
    address: str,
    display_name: str | None = None,
    quota_mb: int = 1024,
    allow_additional_identity: bool = False,
) -> tuple[Mailbox, str]:
    """Create a sender identity. Returns (mailbox, generated_password).

    The password is generated here, stored encrypted, and returned once so it can
    be verified. It is never logged.
    """
    address = address.strip().lower()
    if "@" not in address:
        raise ProvisioningError(f"not an email address: {address}")

    local_part, _, domain_name = address.partition("@")
    if not local_part:
        raise ProvisioningError(f"missing local part: {address}")

    domain = (
        await session.execute(select(Domain).where(Domain.name == domain_name))
    ).scalar_one_or_none()
    if domain is None:
        raise ProvisioningError(f"domain not tracked: {domain_name}. Add it first.")

    if domain.status in (DomainStatus.ADDED, DomainStatus.OWNERSHIP_PENDING):
        raise ProvisioningError(
            f"domain {domain_name} is {domain.status.value}; publish its DNS records "
            "and re-verify before provisioning a mailbox"
        )

    existing = (
        await session.execute(select(Mailbox).where(Mailbox.address == address))
    ).scalar_one_or_none()
    if existing is not None:
        raise ProvisioningError(f"mailbox already provisioned: {address}")

    # The policy gate. A second mailbox on a domain is legitimate only when it is
    # a genuinely different sender identity, which the caller must assert.
    sibling_count = (
        await session.execute(select(func.count(Mailbox.id)).where(Mailbox.domain_id == domain.id))
    ).scalar_one()
    if sibling_count and not allow_additional_identity:
        siblings = (
            (await session.execute(select(Mailbox.address).where(Mailbox.domain_id == domain.id)))
            .scalars()
            .all()
        )
        raise PolicyViolation(
            f"{domain_name} already has a sender identity ({', '.join(siblings)}). "
            "Creating additional mailboxes to increase the 400/hour budget violates "
            "MXRoute's acceptable-use policy and risks account termination. "
            "If this is a genuinely distinct sender identity, pass "
            "allow_additional_identity=True (CLI: --additional-identity)."
        )

    password = generate_mailbox_password()

    try:
        await client.create_email_account(
            domain_name,
            local_part,
            password,
            quota_mb=quota_mb,
            daily_limit=PROVIDER_DAILY_LIMIT,
        )
    except MXRouteConflict:
        # The mailbox exists at the provider but not locally -- most often a
        # half-finished earlier run. We cannot recover the existing password, so
        # reset it to one we know rather than storing a credential we cannot use.
        log.warning("mailbox %s exists at provider; resetting password to adopt it", address)
        await client.update_email_account(domain_name, local_part, password=password)

    mailbox = Mailbox(
        domain_id=domain.id,
        address=address,
        password_encrypted=cipher.encrypt(password),
        display_name=display_name,
        hourly_limit=PROVIDER_HOURLY_LIMIT,
        daily_limit=PROVIDER_DAILY_LIMIT,
        active=True,
        provisioned_at=func.now(),
    )
    session.add(mailbox)
    await session.flush()

    log.info("provisioned sender identity %s", address)
    return mailbox, password


async def rotate_mailbox_password(
    session: AsyncSession,
    client: MXRouteClient,
    cipher: MailboxCipher,
    address: str,
) -> str:
    """Rotate an SMTP password at the provider and re-encrypt it locally.

    Ordering is deliberate: change it at the provider first, and only persist
    locally once that succeeds. The reverse order would leave the database
    holding a credential that does not work, which breaks sending immediately.
    A failure here leaves the old password valid at both ends.
    """
    mailbox = (
        await session.execute(select(Mailbox).where(Mailbox.address == address))
    ).scalar_one_or_none()
    if mailbox is None:
        raise ProvisioningError(f"mailbox not tracked: {address}")

    local_part, _, domain_name = address.partition("@")
    new_password = generate_mailbox_password()

    await client.update_email_account(domain_name, local_part, password=new_password)
    mailbox.password_encrypted = cipher.encrypt(new_password)

    log.info("rotated SMTP password for %s", address)
    return new_password


async def provider_usage(client: MXRouteClient, address: str) -> dict[str, int]:
    """Live `sent` / `limit` from MXRoute, for drift detection against our own
    accounting. Their counter is authoritative; ours only has to stay below it."""
    local_part, _, domain_name = address.partition("@")
    data = await client.get_email_account(domain_name, local_part)
    return {
        "sent_today": int(data.get("sent", 0)),
        "daily_limit": int(data.get("limit", PROVIDER_DAILY_LIMIT)),
        "suspended": bool(data.get("suspended", False)),
    }
