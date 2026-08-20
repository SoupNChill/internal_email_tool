"""Domain lifecycle.

The state machine from vision.md, with one rule that matters more than the rest:

    A domain is never demoted on the strength of a failed DNS *lookup*.

A resolver timeout is not evidence that a record is gone. Treating it as such
would let a transient network problem suspend a healthy domain and stop real
mail -- converting someone else's outage into ours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.dnscheck import DomainDnsReport, verify_domain
from emaild.models import Domain, DomainStatus, Mailbox
from emaild.providers.mxroute import (
    MXRouteClient,
    MXRouteConflict,
    MXRouteError,
)

log = logging.getLogger(__name__)

# Statuses an operator set deliberately. The automatic sweep must not overwrite
# a human decision.
_OPERATOR_CONTROLLED = frozenset({DomainStatus.SUSPENDED})


@dataclass
class DomainRefresh:
    domain: str
    previous: DomainStatus
    current: DomainStatus
    report: DomainDnsReport | None
    changed: bool
    note: str | None = None


def _next_status(
    current: DomainStatus,
    report: DomainDnsReport,
    has_mailbox: bool,
) -> tuple[DomainStatus, str | None]:
    """Decide the new status from evidence. Pure, so it is cheap to test."""
    if current in _OPERATOR_CONTROLLED:
        return current, "operator-controlled status left untouched"

    ownership = report.checks.get("ownership")
    if ownership is not None and not ownership.ok:
        # An errored ownership lookup is not proof the token is gone.
        if ownership.result.value == "error":
            return current, "ownership lookup failed; status held"
        return DomainStatus.OWNERSHIP_PENDING, "verification TXT not resolving"

    if not report.can_send:
        if report.had_lookup_errors:
            # Some required check errored. Hold rather than demote.
            return current, "DNS lookup errors; status held pending a clean sweep"
        if current is DomainStatus.READY:
            # It used to work and no longer does. This is the case that must be
            # loud: something changed outside the application.
            return (
                DomainStatus.MISCONFIGURED,
                f"was ready; now failing: {', '.join(report.failures)}",
            )
        return DomainStatus.DNS_INCOMPLETE, f"missing: {', '.join(report.failures)}"

    if not has_mailbox:
        return DomainStatus.VERIFIED, "DNS complete; awaiting mailbox provisioning"

    return DomainStatus.READY, None


async def refresh_domain(
    session: AsyncSession,
    client: MXRouteClient,
    domain_name: str,
) -> DomainRefresh:
    """Re-verify one domain against the provider and live DNS, then update state."""
    domain = (
        await session.execute(select(Domain).where(Domain.name == domain_name))
    ).scalar_one_or_none()
    if domain is None:
        raise LookupError(f"domain not tracked locally: {domain_name}")

    previous = domain.status

    try:
        dns_info = await client.get_dns(domain_name)
    except MXRouteError as exc:
        # The provider is unreachable. Say so; do not infer anything about DNS.
        log.warning("domain refresh: provider lookup failed for %s: %s", domain_name, exc)
        return DomainRefresh(domain_name, previous, previous, None, False, f"provider error: {exc}")

    report = await verify_domain(domain_name, dns_info)

    mailbox_count = len(
        (await session.execute(select(Mailbox.id).where(Mailbox.domain_id == domain.id)))
        .scalars()
        .all()
    )
    new_status, note = _next_status(previous, report, mailbox_count > 0)

    # Persist the authoritative SMTP host alongside the verification result. It
    # comes from the provider per domain and is never hardcoded.
    mx_records = dns_info.get("mx_records") or []
    if mx_records:
        domain.smtp_host = mx_records[0].get("hostname")

    # Refreshed on every verify, so a provider-side change (a rotated DKIM key)
    # reaches the dashboard rather than leaving a stale record on screen that
    # the operator dutifully re-publishes.
    domain.required_records = required_dns_records(domain_name, dns_info)

    verification = dns_info.get("verification") or {}
    if verification.get("name"):
        domain.verification_token = verification["name"]

    domain.dns_state = report.to_dict()
    domain.dns_checked_at = datetime.now(UTC)
    domain.status = new_status

    if new_status is not previous:
        log.info(
            "domain %s: %s -> %s (%s)", domain_name, previous.value, new_status.value, note or "ok"
        )

    return DomainRefresh(
        domain_name, previous, new_status, report, new_status is not previous, note
    )


async def add_domain(
    session: AsyncSession,
    client: MXRouteClient,
    domain_name: str,
) -> Domain:
    """Track a domain locally and register it with MXRoute.

    Ordering matters: MXRoute rejects a domain whose verification TXT is not yet
    published, so a failure here usually means "add the TXT record and wait for
    propagation", not "something is broken".
    """
    domain_name = domain_name.strip().lower().rstrip(".")

    existing = (
        await session.execute(select(Domain).where(Domain.name == domain_name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    remote = await client.list_domains()
    if domain_name not in remote:
        try:
            await client.create_domain(domain_name)
            log.info("domain %s created at provider", domain_name)
        except MXRouteConflict:
            # Present at the provider but absent from our list call -- harmless,
            # and adopting it is the right move.
            log.info("domain %s already existed at provider; adopting", domain_name)

    domain = Domain(name=domain_name, status=DomainStatus.ADDED)

    # Capture the records now, while we still hold a provider client. The
    # dashboard has to display these and can never fetch them itself: doing so
    # needs the MXRoute account-root credential, which role=api is not given.
    # A failure here must not fail the add -- the domain is registered either
    # way, and `domains verify` refreshes this.
    try:
        dns_info = await client.get_dns(domain_name)
        domain.required_records = required_dns_records(domain_name, dns_info)
        mx = (dns_info.get("mx_records") or [{}])[0]
        domain.smtp_host = mx.get("hostname")
    except MXRouteError as exc:
        log.warning("could not capture DNS records for %s: %s", domain_name, exc)

    session.add(domain)
    await session.flush()
    return domain


async def get_verification_record(client: MXRouteClient) -> dict[str, str]:
    """The account-wide TXT record required before any domain can be added."""
    data = await client.get_verification_key()
    record = data.get("record") or {}
    return {
        "type": record.get("type", "TXT"),
        "name": record.get("name", ""),
        "value": record.get("value", "domain-verified"),
    }


def required_dns_records(domain_name: str, dns_info: dict) -> list[dict[str, str]]:
    """The exact records an operator must publish, ready to paste into a registrar.

    DMARC is included with a safe starting policy because MXRoute does not supply
    one -- verification would otherwise wait forever for a record that is never
    going to arrive from the provider.
    """
    records: list[dict[str, str]] = []

    for mx in dns_info.get("mx_records") or []:
        records.append(
            {
                "type": "MX",
                "name": "@",
                "value": mx.get("hostname", ""),
                "priority": str(mx.get("priority", 10)),
            }
        )

    spf = dns_info.get("spf") or {}
    if spf.get("value"):
        records.append({"type": "TXT", "name": "@", "value": spf["value"], "priority": ""})

    dkim = dns_info.get("dkim") or {}
    if dkim.get("value"):
        # Strip the provider's escaped quoting: pasting those quotes into a
        # registrar yields a record that resolves fine and fails verification.
        records.append(
            {
                "type": "TXT",
                "name": dkim.get("name", "x._domainkey"),
                "value": dkim["value"].strip('"').replace('\\"', ""),
                "priority": "",
            }
        )

    verification = dns_info.get("verification") or {}
    if verification.get("name"):
        records.append(
            {
                "type": "TXT",
                "name": verification["name"],
                "value": verification.get("value", "domain-verified"),
                "priority": "",
            }
        )

    records.append({"type": "TXT", "name": "_dmarc", "value": "v=DMARC1; p=none;", "priority": ""})
    return records
