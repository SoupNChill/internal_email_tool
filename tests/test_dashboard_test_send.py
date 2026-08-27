"""Sending a test message from the dashboard.

The behaviour under test is mostly about what this must NOT become. A page that
sends mail on the operator's behalf is one careless step from being a mail
client, and two from being an open relay on the LAN, so the tests here pin the
boundaries rather than the happy path:

  - the sender must be one the chosen key is actually scoped to, even though
    the form only offers valid pairs (a form is a convenience, never a control)
  - a revoked key cannot send, because the option list is rendered from state
    that may be minutes old
  - the recipient is the ONLY caller-supplied content

The happy path is here too, but it stops at `queued` -- there is no worker in
these tests, and pretending otherwise would test the wrong thing anyway.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.crypto import generate_api_key
from emaild.dashboard import csrf
from emaild.dashboard.testsend import (
    SendRefused,
    blocked_senders,
    parse_option,
    send_options,
    send_test_message,
)
from emaild.models import (
    ApiKey,
    ApiKeyScope,
    Base,
    Domain,
    DomainStatus,
    Mailbox,
    Message,
    Project,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

TOKEN = "dashboard-test-token-value"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"x:{TOKEN}".encode()).decode()}

READY_SENDER = "noreply@example.com"
OTHER_SENDER = "billing@example.com"

_IDS: dict[str, int] = {}


@pytest.fixture
async def engine():
    engine = create_async_engine(TEST_DSN, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def seeded(engine):
    """One ready domain, two senders, and a key scoped to only the first.

    The second sender exists precisely so that "scoped to" can be distinguished
    from "exists" -- a check that passes trivially when there is only one
    mailbox in the database.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        domain = Domain(name="example.com", status=DomainStatus.READY)
        project = Project(name="billing", active=True)
        session.add_all([domain, project])
        await session.flush()

        first = Mailbox(
            domain_id=domain.id, address=READY_SENDER, password_encrypted="x", active=True
        )
        second = Mailbox(
            domain_id=domain.id, address=OTHER_SENDER, password_encrypted="x", active=True
        )
        session.add_all([first, second])
        await session.flush()

        _, digest, prefix = generate_api_key()
        key = ApiKey(project_id=project.id, name="saas-prod", key_hash=digest, key_prefix=prefix)
        session.add(key)
        await session.flush()
        session.add(ApiKeyScope(api_key_id=key.id, mailbox_id=first.id))
        await session.commit()

        _IDS.update(key=key.id, project=project.id, other_mailbox=second.id)
    yield maker


@pytest.fixture
def client(seeded, monkeypatch):
    monkeypatch.setenv("EMAILD_ROLE", "api")
    monkeypatch.setenv("EMAILD_DATABASE_URL", TEST_DSN or "")
    monkeypatch.setenv("EMAILD_DASHBOARD_TOKEN", TOKEN)
    from emaild.config import get_settings

    get_settings.cache_clear()
    from emaild.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _send(client, **data):
    from emaild.config import get_settings

    body = {"csrf_token": csrf.issue_token(get_settings()), **data}
    return client.post("/test/send", data=body, headers=AUTH, follow_redirects=False)


async def _messages(maker) -> list[Message]:
    async with maker() as session:
        return list((await session.execute(select(Message))).scalars().all())


async def _send_direct(maker, **over):
    """Call the module directly, bypassing the form, as a hostile client would."""
    args = {
        "option": f"{_IDS['key']}|{READY_SENDER}",
        "recipient": "operator@example.net",
        "base_url": "http://prod1:8000",
        "body_retention_hours": 72,
        "idempotency_ttl_hours": 24,
    }
    args.update(over)
    async with maker() as session:
        result = await send_test_message(session, **args)
        await session.commit()
        return result


# --- what the page offers --------------------------------------------------


async def test_only_scoped_pairs_are_offered(seeded):
    async with seeded() as session:
        options = await send_options(session)
    assert [o.sender for o in options] == [READY_SENDER]
    assert options[0].key_name == "saas-prod"


async def test_a_mailbox_on_an_unready_domain_is_not_offered(engine, seeded):
    """A domain that is not READY cannot send, so offering it would produce a
    `domain_not_ready` error the page could have predicted."""
    async with seeded() as session:
        domain = (await session.execute(select(Domain))).scalar_one()
        domain.status = DomainStatus.DNS_INCOMPLETE
        await session.commit()

    async with seeded() as session:
        assert await send_options(session) == []


async def test_a_revoked_key_is_not_offered(seeded):
    async with seeded() as session:
        key = (await session.execute(select(ApiKey))).scalar_one()
        key.revoked_at = datetime.now(UTC)
        await session.commit()

    async with seeded() as session:
        assert await send_options(session) == []


# --- why something is NOT offered ------------------------------------------
#
# The dropdown filters silently, and the first operator to add a second domain
# read the short list as a bug in emaild rather than as unfinished setup. It was
# a reasonable reading: nothing on the page distinguished the two. These pin
# that every way an address can be excluded is also explained.


async def test_a_sender_with_no_key_scoped_to_it_is_explained(seeded):
    """The seed has two mailboxes and a key scoped to only one. The unscoped
    one must be named, not merely absent."""
    async with seeded() as session:
        blocked = await blocked_senders(session)

    entry = next(b for b in blocked if b.subject == OTHER_SENDER)
    assert "no active api key" in entry.reason.lower()
    assert entry.href == "/keys"
    # Scopes are set once, at creation -- there is no way to add one to an
    # existing key, so the fix must not suggest editing one.
    assert "existing" not in entry.fix.lower()


async def test_a_sender_on_an_unready_domain_names_the_domain_status(seeded):
    async with seeded() as session:
        domain = (await session.execute(select(Domain))).scalar_one()
        domain.status = DomainStatus.VERIFIED
        await session.commit()

    async with seeded() as session:
        blocked = await blocked_senders(session)

    entry = next(b for b in blocked if b.subject == READY_SENDER)
    assert "verified" in entry.reason
    assert "example.com" in entry.reason
    assert entry.href == "/domains"


async def test_only_the_first_blocker_is_reported_per_address(seeded):
    """An unverified domain AND no key is one task, not two -- the second is not
    actionable until the first is done."""
    async with seeded() as session:
        domain = (await session.execute(select(Domain))).scalar_one()
        domain.status = DomainStatus.DNS_INCOMPLETE
        await session.commit()

    async with seeded() as session:
        blocked = await blocked_senders(session)

    for address in (READY_SENDER, OTHER_SENDER):
        entries = [b for b in blocked if b.subject == address]
        assert len(entries) == 1
        assert "dns_incomplete" in entries[0].reason


async def test_a_ready_domain_with_no_sender_carries_the_provision_command(engine, seeded):
    """The most common shape for a brand-new domain: nothing to list per
    address, so the domain itself has to be named."""
    async with seeded() as session:
        session.add(Domain(name="newdomain.com", status=DomainStatus.READY))
        await session.commit()

    async with seeded() as session:
        blocked = await blocked_senders(session)

    entry = next(b for b in blocked if b.subject == "newdomain.com")
    assert "no sender identity" in entry.reason.lower()
    assert entry.command == "appctl admin mailboxes provision noreply@newdomain.com"


async def test_a_fully_working_sender_is_not_reported_as_blocked(seeded):
    async with seeded() as session:
        blocked = await blocked_senders(session)
    assert READY_SENDER not in [b.subject for b in blocked]


# --- the boundary the form does not enforce --------------------------------


async def test_sending_as_an_unscoped_sender_is_refused(seeded):
    """The form never offers this pairing. The backend must refuse it anyway."""
    with pytest.raises(SendRefused):
        await _send_direct(seeded, option=f"{_IDS['key']}|{OTHER_SENDER}")
    assert await _messages(seeded) == []


async def test_a_revoked_key_cannot_send(seeded):
    """The option list is rendered from state that may be stale by the time the
    form is submitted -- revocation has to be checked at use, not at render."""
    async with seeded() as session:
        key = (await session.execute(select(ApiKey))).scalar_one()
        key.revoked_at = datetime.now(UTC)
        await session.commit()

    with pytest.raises(SendRefused):
        await _send_direct(seeded)
    assert await _messages(seeded) == []


async def test_an_unknown_key_id_is_refused(seeded):
    with pytest.raises(SendRefused):
        await _send_direct(seeded, option=f"{_IDS['key'] + 999}|{READY_SENDER}")


@pytest.mark.parametrize("bad", ["", "|", "abc|x@y.com", "5|", "noreply@example.com"])
async def test_a_malformed_option_is_refused(seeded, bad):
    with pytest.raises(SendRefused):
        await _send_direct(seeded, option=bad)


async def test_an_empty_recipient_is_refused(seeded):
    with pytest.raises(SendRefused):
        await _send_direct(seeded, recipient="   ")


# --- the happy path --------------------------------------------------------


async def test_a_test_message_is_queued_and_attributed_to_the_key(seeded):
    public_id = await _send_direct(seeded)
    assert public_id.startswith("email_")

    messages = await _messages(seeded)
    assert len(messages) == 1
    m = messages[0]
    assert m.from_address == READY_SENDER
    assert m.to_addresses == ["operator@example.net"]
    assert m.project_id == _IDS["project"]
    assert m.api_key_id == _IDS["key"]


async def test_the_message_identifies_itself_and_is_timestamped(seeded):
    """Operators send several during a DNS problem. A subject that does not
    distinguish one attempt from the next is how an old message gets mistaken
    for proof that the new one worked."""
    await _send_direct(seeded)
    m = (await _messages(seeded))[0]
    assert "emaild test" in m.subject.lower()
    assert datetime.now(UTC).strftime("%Y-%m-%d") in m.subject


async def test_a_test_send_does_not_mark_the_key_as_used(seeded):
    """`last_used_at` answers "is my application using this key?". A dashboard
    test is not the application, and letting it set that column would make an
    unused key look live."""
    await _send_direct(seeded)
    async with seeded() as session:
        key = (await session.execute(select(ApiKey))).scalar_one()
    assert key.last_used_at is None


async def test_a_crafted_host_header_cannot_inject_markup(seeded):
    """`base_url` comes from the Host header and lands in an HTML mail body we
    sign with our own DKIM key. Jinja never sees this string, so the escaping
    has to happen here."""
    await _send_direct(seeded, base_url="http://x/<img src=x onerror=alert(1)>")
    m = (await _messages(seeded))[0]
    assert "<img" not in m.body_html
    assert "&lt;img" in m.body_html


async def test_two_sends_produce_two_messages(seeded):
    """No idempotency key: clicking twice means the operator wants two, and
    collapsing them would look exactly like the second one failing."""
    first = await _send_direct(seeded)
    second = await _send_direct(seeded)
    assert first != second
    assert len(await _messages(seeded)) == 2


# --- through HTTP ----------------------------------------------------------


def test_the_form_renders_the_available_pairs(client):
    page = client.get("/test", headers=AUTH)
    assert page.status_code == 200
    assert page.text.count(f'value="{_IDS["key"]}|{READY_SENDER}"') == 1
    assert "saas-prod" in page.text
    # The unscoped sender must not be SELECTABLE. It does still appear on the
    # page, in the panel that explains why -- see the test below.
    assert f"|{OTHER_SENDER}" not in page.text


def test_the_page_explains_an_address_it_does_not_offer(client):
    """The whole point: a short dropdown must not be silent. The unscoped
    sender is absent from the <select> and present in the explanation."""
    page = client.get("/test", headers=AUTH).text
    assert f'value="{_IDS["key"]}|{OTHER_SENDER}"' not in page
    assert OTHER_SENDER in page
    assert "Not available to send from" in page


def test_a_successful_send_redirects_to_the_message_timeline(client):
    """Not back to the form with a green tick: `queued` is not `sent`, and the
    timeline is the page that says so."""
    response = _send(client, option=f"{_IDS['key']}|{READY_SENDER}", recipient="op@example.net")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/messages/email_")


def test_a_refused_send_returns_to_the_form(client):
    response = _send(client, option=f"{_IDS['key']}|{OTHER_SENDER}", recipient="op@example.net")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/test?")


def test_parse_option_round_trips():
    assert parse_option("7|a@b.com") == (7, "a@b.com")
