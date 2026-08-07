"""Phase 2 unit tests.

Deliberately no network: the state machine, DNS parsing, crypto, and rate
limiting are all testable in isolation, and tests that need a live provider are
not tests -- they are a live verification run, done separately.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from emaild.crypto import (
    DecryptionError,
    MailboxCipher,
    generate_api_key,
    generate_mailbox_password,
    hash_api_key,
    verify_api_key,
)
from emaild.dnscheck import CheckResult, DomainDnsReport, RecordCheck, _normalise_txt
from emaild.domains import _next_status, required_dns_records
from emaild.models import DomainStatus
from emaild.providers.mxroute import (
    MXRouteAPIError,
    MXRouteAuthError,
    MXRouteClient,
    MXRouteConflict,
    MXRouteUnavailable,
    RateLimiter,
)

# Throwaway fixture key. MUST NOT match any real EMAILD_MAILBOX_ENCRYPTION_KEY:
# a test constant that also guards live data puts that key in git history.
FERNET_KEY = "Rfzds2IQniNlIxTgv8hDeOafJ9T2Jg4nq2vZbiN-rQM="


# --- crypto ----------------------------------------------------------------


def test_generated_password_satisfies_provider_complexity_rule():
    for _ in range(200):
        pw = generate_mailbox_password()
        assert len(pw) >= 8
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert pw.isalnum()  # no shell/quote characters


def test_generated_passwords_are_unique():
    assert len({generate_mailbox_password() for _ in range(500)}) == 500


def test_api_key_hash_is_stable_and_verifies():
    key, digest, prefix = generate_api_key()
    assert key.startswith("em_live_")
    assert prefix == key[:14]
    assert hash_api_key(key) == digest
    assert verify_api_key(key, digest)
    assert not verify_api_key(key + "x", digest)


def test_api_key_is_not_recoverable_from_hash():
    key, digest, _ = generate_api_key()
    assert key not in digest
    assert len(digest) == 64


def test_mailbox_cipher_roundtrip():
    cipher = MailboxCipher(FERNET_KEY)
    secret = generate_mailbox_password()
    assert cipher.decrypt(cipher.encrypt(secret)) == secret


def test_ciphertext_differs_each_time():
    """Fernet includes a random IV, so identical plaintext must not produce
    identical ciphertext -- otherwise equal passwords are visibly equal in the DB."""
    cipher = MailboxCipher(FERNET_KEY)
    assert cipher.encrypt("same") != cipher.encrypt("same")


def test_wrong_key_raises_and_leaks_nothing():
    other = Fernet.generate_key().decode()
    blob = MailboxCipher(FERNET_KEY).encrypt("hunter2")
    with pytest.raises(DecryptionError) as exc:
        MailboxCipher(other).decrypt(blob)
    assert "hunter2" not in str(exc.value)


# --- DNS parsing -----------------------------------------------------------


def test_normalise_handles_split_and_quoted_txt():
    """A 2048-bit DKIM key exceeds the 255-byte TXT limit, so resolvers return
    it chunked and quoted in several ways. All must compare equal."""
    canonical = "v=DKIM1; k=rsa; p=MIIBIjANBg"
    variants = [
        '"v=DKIM1; k=rsa; p=MIIBIjANBg"',
        '"v=DKIM1; k=rsa; " "p=MIIBIjANBg"',
        "v=DKIM1;k=rsa;p=MIIBIjANBg",
        '\\"v=DKIM1; k=rsa; p=MIIBIjANBg\\"',
    ]
    for v in variants:
        assert _normalise_txt(v) == _normalise_txt(canonical)


def _report(**results: CheckResult) -> DomainDnsReport:
    return DomainDnsReport(
        domain="example.com",
        checks={k: RecordCheck(k, v) for k, v in results.items()},
    )


def test_can_send_requires_mx_spf_dkim_but_not_dmarc():
    report = _report(
        mx=CheckResult.PASS,
        spf=CheckResult.PASS,
        dkim=CheckResult.PASS,
        dmarc=CheckResult.MISSING,
    )
    assert report.can_send is True


def test_missing_dkim_blocks_sending():
    report = _report(mx=CheckResult.PASS, spf=CheckResult.PASS, dkim=CheckResult.MISSING)
    assert report.can_send is False
    assert "dkim" in report.failures


# --- state machine ---------------------------------------------------------


def test_healthy_domain_with_mailbox_becomes_ready():
    report = _report(
        ownership=CheckResult.PASS, mx=CheckResult.PASS, spf=CheckResult.PASS, dkim=CheckResult.PASS
    )
    status, _ = _next_status(DomainStatus.VERIFIED, report, has_mailbox=True)
    assert status is DomainStatus.READY


def test_healthy_domain_without_mailbox_stops_at_verified():
    report = _report(
        ownership=CheckResult.PASS, mx=CheckResult.PASS, spf=CheckResult.PASS, dkim=CheckResult.PASS
    )
    status, note = _next_status(DomainStatus.DNS_INCOMPLETE, report, has_mailbox=False)
    assert status is DomainStatus.VERIFIED
    assert note and "mailbox" in note


def test_ready_domain_that_breaks_becomes_misconfigured():
    """The loud case: it used to work, so something changed outside the app."""
    report = _report(
        ownership=CheckResult.PASS, mx=CheckResult.PASS, spf=CheckResult.PASS, dkim=CheckResult.FAIL
    )
    status, note = _next_status(DomainStatus.READY, report, has_mailbox=True)
    assert status is DomainStatus.MISCONFIGURED
    assert note and "dkim" in note


def test_lookup_error_never_demotes_a_ready_domain():
    """A resolver timeout is not evidence a record vanished. Demoting on it would
    turn someone else's outage into ours."""
    report = _report(
        ownership=CheckResult.PASS,
        mx=CheckResult.PASS,
        spf=CheckResult.PASS,
        dkim=CheckResult.ERROR,
    )
    status, note = _next_status(DomainStatus.READY, report, has_mailbox=True)
    assert status is DomainStatus.READY
    assert note and "held" in note


def test_ownership_error_holds_status():
    report = _report(
        ownership=CheckResult.ERROR,
        mx=CheckResult.PASS,
        spf=CheckResult.PASS,
        dkim=CheckResult.PASS,
    )
    status, _ = _next_status(DomainStatus.READY, report, has_mailbox=True)
    assert status is DomainStatus.READY


def test_missing_ownership_returns_to_pending():
    report = _report(
        ownership=CheckResult.MISSING,
        mx=CheckResult.PASS,
        spf=CheckResult.PASS,
        dkim=CheckResult.PASS,
    )
    status, _ = _next_status(DomainStatus.READY, report, has_mailbox=True)
    assert status is DomainStatus.OWNERSHIP_PENDING


def test_suspended_is_never_overwritten_by_the_sweep():
    report = _report(
        ownership=CheckResult.PASS, mx=CheckResult.PASS, spf=CheckResult.PASS, dkim=CheckResult.PASS
    )
    status, _ = _next_status(DomainStatus.SUSPENDED, report, has_mailbox=True)
    assert status is DomainStatus.SUSPENDED


# --- record rendering ------------------------------------------------------


def test_required_records_strip_provider_quoting_and_add_dmarc():
    dns_info = {
        "mx_records": [{"priority": 10, "hostname": "chocobo.mxrouting.net"}],
        "spf": {"value": "v=spf1 include:mxroute.com -all"},
        "dkim": {"name": "x._domainkey", "value": '"v=DKIM1; k=rsa; p=AAAA"'},
        "verification": {"name": "_da-verify-abc", "value": "domain-verified"},
    }
    records = required_dns_records("example.com", dns_info)
    # Keyed by (type, name): MX and SPF both live at "@", so name alone collides.
    by_key = {(r["type"], r["name"]): r for r in records}

    # Quotes must be stripped: pasting them yields a record that resolves but
    # fails DKIM verification.
    assert by_key[("TXT", "x._domainkey")]["value"] == "v=DKIM1; k=rsa; p=AAAA"
    # DMARC is ours to author -- MXRoute never returns one.
    assert by_key[("TXT", "_dmarc")]["value"].startswith("v=DMARC1")
    assert by_key[("MX", "@")]["priority"] == "10"
    assert by_key[("TXT", "@")]["value"].startswith("v=spf1")
    # Both apex records must survive rendering; neither may shadow the other.
    assert len(records) == 5


# --- rate limiting ---------------------------------------------------------


async def test_reads_and_writes_have_separate_budgets():
    limiter = RateLimiter(read_limit=5, write_limit=2)
    for _ in range(5):
        await limiter.acquire("GET")
    for _ in range(2):
        await limiter.acquire("POST")
    # Both budgets exhausted independently; neither borrowed from the other.
    assert len(limiter._reads) == 5
    assert len(limiter._writes) == 2


async def test_limiter_blocks_once_budget_is_spent():
    limiter = RateLimiter(read_limit=2, write_limit=2)
    await limiter.acquire("GET")
    await limiter.acquire("GET")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire("GET"), timeout=0.3)


async def test_write_budget_unaffected_by_read_saturation():
    limiter = RateLimiter(read_limit=1, write_limit=1)
    await limiter.acquire("GET")
    start = time.monotonic()
    await limiter.acquire("DELETE")  # must not wait on the read window
    assert time.monotonic() - start < 0.1


# --- client error mapping --------------------------------------------------


def _client(handler) -> MXRouteClient:
    return MXRouteClient(
        "srv", "user", "key", transport=httpx.MockTransport(handler), limiter=RateLimiter()
    )


async def test_unwraps_the_data_envelope():
    async with _client(
        lambda r: httpx.Response(200, json={"success": True, "data": ["a.com", "b.com"]})
    ) as c:
        assert await c.list_domains() == ["a.com", "b.com"]


async def test_auth_failure_is_typed_and_not_retried_as_generic():
    async with _client(lambda r: httpx.Response(401, json={"success": False})) as c:
        with pytest.raises(MXRouteAuthError):
            await c.list_domains()


async def test_conflict_is_distinguishable_from_other_errors():
    async with _client(lambda r: httpx.Response(409, json={"success": False})) as c:
        with pytest.raises(MXRouteConflict):
            await c.create_domain("x.com")


async def test_documented_but_unimplemented_endpoint_degrades():
    """GET /domains/{d}/mail-status is in the spec and answers 405 live. The
    client must surface that as 'unavailable', not as a hard failure."""
    async with _client(
        lambda r: httpx.Response(
            405, json={"success": False, "error": {"code": "METHOD_NOT_ALLOWED"}}
        )
    ) as c:
        with pytest.raises(MXRouteUnavailable):
            await c._request("GET", "/domains/x.com/mail-status")


async def test_structured_api_error_is_surfaced_with_its_code():
    async with _client(
        lambda r: httpx.Response(
            400,
            json={
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "bad password",
                    "field": "password",
                },
            },
        )
    ) as c:
        with pytest.raises(MXRouteAPIError) as exc:
            await c.create_domain("x.com")
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.field_name == "password"


async def test_transient_failure_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"success": True, "data": []})

    async with _client(handler) as c:
        assert await c.list_domains() == []
    assert calls["n"] == 3


async def test_update_account_requires_at_least_one_field():
    async with _client(lambda r: httpx.Response(200, json={"success": True})) as c:
        with pytest.raises(ValueError):
            await c.update_email_account("x.com", "user")


# --- env file selection ----------------------------------------------------


def test_env_file_selection_honours_override(monkeypatch):
    """A host-side worker must be able to ignore the shared .env, which holds
    every role's variables for Compose to distribute."""
    from emaild.config import _env_file_for_process

    monkeypatch.delenv("EMAILD_ENV_FILE", raising=False)
    assert _env_file_for_process() == ".env"

    monkeypatch.setenv("EMAILD_ENV_FILE", "none")
    assert _env_file_for_process() is None

    monkeypatch.setenv("EMAILD_ENV_FILE", ".env.worker")
    assert _env_file_for_process() == ".env.worker"
