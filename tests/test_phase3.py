"""Phase 3 authorization tests.

Runs against a real Postgres because authorization is a database question --
mocking the lookup would test the mock. The database URL comes from
EMAILD_TEST_DATABASE_URL; these tests skip when it is absent so the suite still
runs on a machine without one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.auth import (
    Principal,
    authorize_sender,
    parse_from_address,
    resolve_principal,
)
from emaild.crypto import generate_api_key
from emaild.errors import AuthenticationError, AuthorizationError, DomainNotReady
from emaild.models import (
    ApiKey,
    ApiKeyScope,
    Base,
    Domain,
    DomainStatus,
    Mailbox,
    Project,
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


async def _fixture(
    session,
    *,
    domain_status: DomainStatus = DomainStatus.READY,
    mailbox_active: bool = True,
    key_revoked: bool = False,
    key_active: bool = True,
    project_active: bool = True,
    scoped: bool = True,
) -> str:
    """Build a complete authorization chain and return the plaintext key."""
    project = Project(name="billing", active=project_active)
    domain = Domain(
        name="example.com",
        status=domain_status,
        dns_state={"checks": {"dkim": {"result": "fail"}, "mx": {"result": "pass"}}},
    )
    session.add_all([project, domain])
    await session.flush()

    mailbox = Mailbox(
        domain_id=domain.id,
        address="noreply@example.com",
        password_encrypted="x",
        active=mailbox_active,
    )
    session.add(mailbox)
    await session.flush()

    full_key, digest, prefix = generate_api_key()
    key = ApiKey(
        project_id=project.id,
        name="k1",
        key_hash=digest,
        key_prefix=prefix,
        active=key_active,
        revoked_at=datetime.now(UTC) if key_revoked else None,
    )
    session.add(key)
    await session.flush()
    if scoped:
        session.add(ApiKeyScope(api_key_id=key.id, mailbox_id=mailbox.id))
    await session.commit()
    return full_key


# --- authentication --------------------------------------------------------


async def test_valid_key_resolves_to_its_project_and_senders(session):
    key = await _fixture(session)
    principal = await resolve_principal(session, key)
    assert principal.project_name == "billing"
    assert principal.allowed_addresses == ["noreply@example.com"]
    assert principal.allowed_domains == {"example.com"}


async def test_unknown_key_is_rejected(session):
    await _fixture(session)
    other, _, _ = generate_api_key()
    with pytest.raises(AuthenticationError):
        await resolve_principal(session, other)


async def test_revoked_key_stops_working_immediately(session):
    """Revocation is why authentication is never cached."""
    key = await _fixture(session, key_revoked=True)
    with pytest.raises(AuthenticationError):
        await resolve_principal(session, key)


async def test_deactivated_key_is_rejected(session):
    key = await _fixture(session, key_active=False)
    with pytest.raises(AuthenticationError):
        await resolve_principal(session, key)


async def test_key_of_inactive_project_is_rejected(session):
    key = await _fixture(session, project_active=False)
    with pytest.raises(AuthenticationError):
        await resolve_principal(session, key)


async def test_rejection_messages_are_indistinguishable(session):
    """Unknown, revoked, and inactive must read identically -- otherwise the
    error message becomes an oracle for which keys exist."""
    revoked = await _fixture(session, key_revoked=True)
    unknown, _, _ = generate_api_key()

    messages = set()
    for candidate in (revoked, unknown):
        with pytest.raises(AuthenticationError) as exc:
            await resolve_principal(session, candidate)
        messages.add(str(exc.value))

    assert len(messages) == 1, f"messages differ and leak key state: {messages}"


async def test_last_used_is_recorded(session):
    key = await _fixture(session)
    principal = await resolve_principal(session, key)
    await session.commit()
    row = await session.get(ApiKey, principal.api_key_id)
    assert row is not None and row.last_used_at is not None


async def test_last_used_is_not_rewritten_on_every_request(session):
    """A write per request is real amplification for a field whose useful
    resolution is 'today'."""
    key = await _fixture(session)
    p = await resolve_principal(session, key)
    await session.commit()
    row = await session.get(ApiKey, p.api_key_id)
    first = row.last_used_at

    await resolve_principal(session, key)
    await session.commit()
    await session.refresh(row)
    assert row.last_used_at == first

    row.last_used_at = datetime.now(UTC) - timedelta(minutes=5)
    await session.commit()
    await resolve_principal(session, key)
    await session.commit()
    await session.refresh(row)
    assert row.last_used_at != first


# --- authorization ---------------------------------------------------------


async def test_authorized_sender_returns_its_mailbox(session):
    key = await _fixture(session)
    principal = await resolve_principal(session, key)
    name, mailbox = authorize_sender(principal, "Acme <noreply@example.com>")
    assert name == "Acme"
    assert mailbox.address == "noreply@example.com"


async def test_sender_outside_scope_is_refused_and_told_what_is_allowed(session):
    key = await _fixture(session)
    principal = await resolve_principal(session, key)
    with pytest.raises(AuthorizationError) as exc:
        authorize_sender(principal, "support@example.com")
    # The caller already authenticated, so naming the permitted senders turns a
    # guessing game into a one-line fix.
    assert "noreply@example.com" in str(exc.value)


async def test_key_with_no_scopes_is_refused_with_guidance(session):
    key = await _fixture(session, scoped=False)
    principal = await resolve_principal(session, key)
    with pytest.raises(AuthorizationError) as exc:
        authorize_sender(principal, "noreply@example.com")
    assert "no sender identities" in str(exc.value)


async def test_deactivated_mailbox_is_refused(session):
    key = await _fixture(session, mailbox_active=False)
    principal = await resolve_principal(session, key)
    with pytest.raises(AuthorizationError):
        authorize_sender(principal, "noreply@example.com")


@pytest.mark.parametrize(
    "status",
    [
        DomainStatus.ADDED,
        DomainStatus.OWNERSHIP_PENDING,
        DomainStatus.DNS_INCOMPLETE,
        DomainStatus.VERIFIED,
        DomainStatus.MISCONFIGURED,
        DomainStatus.SUSPENDED,
    ],
)
async def test_only_ready_domains_may_send(session, status):
    key = await _fixture(session, domain_status=status)
    principal = await resolve_principal(session, key)
    with pytest.raises(DomainNotReady):
        authorize_sender(principal, "noreply@example.com")


async def test_not_ready_error_names_the_failing_checks(session):
    """A different failure with a different fix: publish DNS, do not widen the key."""
    key = await _fixture(session, domain_status=DomainStatus.DNS_INCOMPLETE)
    principal = await resolve_principal(session, key)
    with pytest.raises(DomainNotReady) as exc:
        authorize_sender(principal, "noreply@example.com")
    assert "dkim" in str(exc.value)


# --- address parsing (no database) -----------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_name", "expected_addr"),
    [
        ("noreply@example.com", None, "noreply@example.com"),
        ("Acme <noreply@example.com>", "Acme", "noreply@example.com"),
        ("  NoReply@Example.COM  ", None, "noreply@example.com"),
        ('"Acme, Inc." <noreply@example.com>', "Acme, Inc.", "noreply@example.com"),
    ],
)
def test_from_parsing_is_resend_compatible(raw, expected_name, expected_addr):
    name, addr = parse_from_address(raw)
    assert name == expected_name
    assert addr == expected_addr


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not an address",
        "Acme <>",
        "@example.com ",  # no local part
        "noreply@localhost",  # no TLD
        "noreply@.com",  # empty label
    ],
)
def test_unparseable_from_is_rejected(raw):
    with pytest.raises(AuthorizationError):
        parse_from_address(raw)


def test_display_name_does_not_widen_authorization():
    """Spoofing via the display name must not work -- the address is what counts."""
    principal = Principal(api_key_id=1, key_name="k", project_id=1, project_name="p", mailboxes={})
    with pytest.raises(AuthorizationError):
        authorize_sender(principal, "noreply@example.com <attacker@evil.com>")


# --- remedies in the not-ready error ---------------------------------------
#
# Found on a first real install: the documented sequence is verify, provision,
# send. Verifying before a mailbox exists correctly leaves the domain at
# 'verified', provisioning did not recompute it, and the send was refused on a
# domain whose DNS had been correct all along. The status was accurate and the
# message still left the operator stuck.


async def test_verified_domain_says_how_to_reach_ready(session):
    key = await _fixture(session, domain_status=DomainStatus.VERIFIED)
    principal = await resolve_principal(session, key)
    with pytest.raises(DomainNotReady) as exc:
        authorize_sender(principal, "noreply@example.com")
    message = str(exc.value)
    assert "domains verify" in message
    assert "DNS is complete" in message


async def test_added_domain_is_told_to_publish_records_first(session):
    key = await _fixture(session, domain_status=DomainStatus.ADDED)
    principal = await resolve_principal(session, key)
    with pytest.raises(DomainNotReady) as exc:
        authorize_sender(principal, "noreply@example.com")
    assert "domains records" in str(exc.value)


@pytest.mark.parametrize(
    "status",
    [
        DomainStatus.ADDED,
        DomainStatus.OWNERSHIP_PENDING,
        DomainStatus.DNS_INCOMPLETE,
        DomainStatus.VERIFIED,
        DomainStatus.MISCONFIGURED,
        DomainStatus.SUSPENDED,
    ],
)
async def test_every_unsendable_status_explains_itself(session, status):
    """Naming the state without naming the remedy makes the caller translate
    one into the other, which is what the docs are for and what an error
    message should spare them."""
    key = await _fixture(session, domain_status=status)
    principal = await resolve_principal(session, key)
    with pytest.raises(DomainNotReady) as exc:
        authorize_sender(principal, "noreply@example.com")
    message = str(exc.value)
    # Beyond the bare "Domain X is 'status' and cannot send." plus the failing
    # checks the fixture always produces.
    assert len(message) > 90, f"{status.value} has no remedy: {message}"
