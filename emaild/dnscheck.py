"""DNS verification: does the world agree with what MXRoute says it needs?

Two independent facts must both hold before a domain may send, and conflating
them is a classic source of "DKIM is configured but mail still fails":

1. MXRoute has generated the records (what `GET /domains/{d}/dns` returns).
2. Those records are actually published and resolving (what this module checks).

The provider populating a DKIM field proves only the first. Only a live lookup
proves the second.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

import dns.asyncresolver
import dns.exception
import dns.rdatatype
import dns.rdtypes.ANY.MX
import dns.rdtypes.ANY.TXT
import dns.resolver

log = logging.getLogger(__name__)

DNS_TIMEOUT = 10.0


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    MISSING = "missing"
    ERROR = "error"  # lookup itself failed -- absence NOT proven


@dataclass
class RecordCheck:
    name: str
    result: CheckResult
    expected: str | None = None
    found: list[str] = field(default_factory=list)
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is CheckResult.PASS


@dataclass
class DomainDnsReport:
    domain: str
    checks: dict[str, RecordCheck]

    # MX, SPF, and DKIM are required to send correctly. DMARC is a policy
    # statement rather than an authentication mechanism -- its absence does not
    # break SPF or DKIM, so it is reported prominently but does not block.
    REQUIRED = ("mx", "spf", "dkim")
    ADVISORY = ("dmarc",)

    @property
    def can_send(self) -> bool:
        return all(self.checks[k].ok for k in self.REQUIRED if k in self.checks)

    @property
    def had_lookup_errors(self) -> bool:
        """True if any check errored.

        Matters because an errored lookup is not evidence of a missing record.
        Demoting a healthy domain because a resolver hiccuped would be a
        self-inflicted outage.
        """
        return any(c.result is CheckResult.ERROR for c in self.checks.values())

    @property
    def failures(self) -> list[str]:
        return [k for k in self.REQUIRED if k in self.checks and not self.checks[k].ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_send": self.can_send,
            "checks": {
                k: {
                    "result": c.result.value,
                    "expected": c.expected,
                    "found": c.found,
                    "detail": c.detail,
                }
                for k, c in self.checks.items()
            },
        }


def _normalise_txt(value: str) -> str:
    """Make two spellings of the same TXT record comparable.

    DNS splits strings longer than 255 bytes into chunks, resolvers re-join them
    with varying quoting, and MXRoute returns DKIM wrapped in escaped quotes.
    Stripping quotes and all whitespace reduces every variant to one form --
    safe here because none of these record types carry meaningful whitespace.
    """
    return value.replace('"', "").replace("\\", "").replace(" ", "").replace("\t", "").strip()


async def _resolve(name: str, rdtype: str) -> tuple[list[str], str | None]:
    """Return (values, error). An empty list with no error means NXDOMAIN."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT
    resolver.timeout = DNS_TIMEOUT
    try:
        answer = await resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return [], None
    except dns.exception.DNSException as exc:
        return [], f"{type(exc).__name__}: {exc}"

    # dnspython's iteration is typed as the abstract Rdata base, which does not
    # declare the concrete per-type attributes. Narrow explicitly rather than
    # silencing it -- the resolver does return the type matching `rdtype`.
    values: list[str] = []
    for record in answer:
        if rdtype == "TXT":
            txt = cast(dns.rdtypes.ANY.TXT.TXT, record)
            values.append("".join(part.decode() for part in txt.strings))
        elif rdtype == "MX":
            mx = cast(dns.rdtypes.ANY.MX.MX, record)
            values.append(f"{mx.preference} {str(mx.exchange).rstrip('.')}")
        else:
            values.append(str(record))
    return values, None


async def check_mx(domain: str, expected_hosts: list[str]) -> RecordCheck:
    values, error = await _resolve(domain, "MX")
    if error:
        return RecordCheck("mx", CheckResult.ERROR, detail=error)
    if not values:
        return RecordCheck("mx", CheckResult.MISSING, expected=", ".join(expected_hosts))

    found_hosts = {v.split(None, 1)[1].lower() for v in values if " " in v}
    # The primary must be present. Extra MX records (a backup relay) are fine;
    # a MISSING primary is not.
    primary = expected_hosts[0].lower().rstrip(".") if expected_hosts else ""
    if primary and primary in found_hosts:
        return RecordCheck("mx", CheckResult.PASS, expected=primary, found=values)
    return RecordCheck(
        "mx",
        CheckResult.FAIL,
        expected=primary,
        found=values,
        detail="primary MX not published",
    )


async def check_spf(domain: str, expected_value: str | None) -> RecordCheck:
    values, error = await _resolve(domain, "TXT")
    if error:
        return RecordCheck("spf", CheckResult.ERROR, detail=error)

    spf_records = [v for v in values if v.strip().lower().startswith("v=spf1")]
    if not spf_records:
        return RecordCheck("spf", CheckResult.MISSING, expected=expected_value)

    if len(spf_records) > 1:
        # More than one SPF record is a hard failure per RFC 7208 -- receivers
        # treat it as permerror, which is worse than having none at all.
        return RecordCheck(
            "spf",
            CheckResult.FAIL,
            expected=expected_value,
            found=spf_records,
            detail="multiple SPF records published; RFC 7208 requires exactly one",
        )

    published = spf_records[0]
    if "include:mxroute.com" not in _normalise_txt(published).lower():
        return RecordCheck(
            "spf",
            CheckResult.FAIL,
            expected=expected_value,
            found=spf_records,
            detail="SPF does not authorise mxroute.com",
        )
    return RecordCheck("spf", CheckResult.PASS, expected=expected_value, found=spf_records)


async def check_dkim(
    domain: str, selector_name: str | None, expected_value: str | None
) -> RecordCheck:
    if not selector_name or not expected_value:
        # MXRoute has not generated a key for this domain yet.
        return RecordCheck(
            "dkim",
            CheckResult.MISSING,
            detail="provider has not generated a DKIM key for this domain",
        )

    fqdn = f"{selector_name}.{domain}" if not selector_name.endswith(domain) else selector_name
    values, error = await _resolve(fqdn, "TXT")
    if error:
        return RecordCheck("dkim", CheckResult.ERROR, expected=fqdn, detail=error)
    if not values:
        return RecordCheck("dkim", CheckResult.MISSING, expected=fqdn)

    want = _normalise_txt(expected_value)
    for published in values:
        if _normalise_txt(published) == want:
            return RecordCheck(
                "dkim", CheckResult.PASS, expected=fqdn, found=[f"{len(published)} chars"]
            )

    # A truncated key resolves fine and fails verification silently -- the single
    # most common DKIM mistake -- so say so explicitly rather than "mismatch".
    detail = "published DKIM does not match the provider's key"
    for published in values:
        if want.startswith(_normalise_txt(published)[:60]) and len(published) < len(expected_value):
            detail = "published DKIM appears truncated (record split or quoted incorrectly)"
            break
    return RecordCheck(
        "dkim",
        CheckResult.FAIL,
        expected=fqdn,
        found=[f"{len(v)} chars" for v in values],
        detail=detail,
    )


async def check_dmarc(domain: str) -> RecordCheck:
    values, error = await _resolve(f"_dmarc.{domain}", "TXT")
    if error:
        return RecordCheck("dmarc", CheckResult.ERROR, detail=error)

    dmarc = [v for v in values if v.strip().lower().startswith("v=dmarc1")]
    if not dmarc:
        return RecordCheck(
            "dmarc",
            CheckResult.MISSING,
            detail="MXRoute does not supply DMARC; author one (start at p=none)",
        )
    return RecordCheck("dmarc", CheckResult.PASS, found=dmarc)


async def check_ownership(domain: str, token_name: str | None) -> RecordCheck:
    if not token_name:
        return RecordCheck("ownership", CheckResult.MISSING, detail="no verification token known")

    fqdn = f"{token_name}.{domain}" if not token_name.endswith(domain) else token_name
    values, error = await _resolve(fqdn, "TXT")
    if error:
        return RecordCheck("ownership", CheckResult.ERROR, expected=fqdn, detail=error)
    if any(_normalise_txt(v) == "domain-verified" for v in values):
        return RecordCheck("ownership", CheckResult.PASS, expected=fqdn)
    return RecordCheck(
        "ownership",
        CheckResult.MISSING if not values else CheckResult.FAIL,
        expected=fqdn,
        found=values,
    )


async def verify_domain(domain: str, dns_info: dict[str, Any]) -> DomainDnsReport:
    """Run every check against the requirements MXRoute reported.

    `dns_info` is the payload from `GET /domains/{domain}/dns`.
    """
    mx_hosts = [r["hostname"] for r in dns_info.get("mx_records") or [] if r.get("hostname")]
    spf = (dns_info.get("spf") or {}).get("value")
    dkim = dns_info.get("dkim") or {}
    verification = dns_info.get("verification") or {}

    mx, spf_check, dkim_check, dmarc, ownership = await asyncio.gather(
        check_mx(domain, mx_hosts),
        check_spf(domain, spf),
        check_dkim(domain, dkim.get("name"), dkim.get("value")),
        check_dmarc(domain),
        check_ownership(domain, verification.get("name")),
    )

    return DomainDnsReport(
        domain=domain,
        checks={
            "mx": mx,
            "spf": spf_check,
            "dkim": dkim_check,
            "dmarc": dmarc,
            "ownership": ownership,
        },
    )
