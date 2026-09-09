"""The dashboard's "what should I do next" logic.

From a first-time user who got everything running and then said: "I don't
really see a clear 'this is what I should do', and I don't understand what a
key or a project is."

These assert the ORDER is right -- each step is only offered once the thing it
depends on exists -- and that steps which cannot be done in the browser say so
with the command to run instead. A guide that suggests an impossible action is
worse than none, and this codebase has produced that exact bug twice.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.dashboard.setup_state import domain_actions, domain_next_action, next_step
from emaild.models import (
    ApiKey,
    Base,
    Domain,
    DomainStatus,
    Mailbox,
    Message,
    Project,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

BASE = "http://prod1:8000"


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


async def _domain(session, status: DomainStatus) -> Domain:
    d = Domain(name="example.com", status=status)
    session.add(d)
    await session.flush()
    return d


async def _mailbox(session, domain: Domain) -> Mailbox:
    m = Mailbox(
        domain_id=domain.id,
        address="noreply@example.com",
        password_encrypted="x",
        active=True,
    )
    session.add(m)
    await session.flush()
    return m


async def test_empty_installation_asks_for_a_domain(session):
    step = await next_step(session, BASE)
    assert "domain" in step.title.lower()
    # In the browser now: the dashboard queues the job, the provisioner runs
    # it. It used to hand over a CLI command.
    assert step.href == "/domains"
    assert not step.done


async def test_added_domain_asks_for_dns(session):
    await _domain(session, DomainStatus.ADDED)
    step = await next_step(session, BASE)
    assert "DNS" in step.title
    assert step.href == "/domains"


async def test_verified_domain_asks_for_a_sender(session):
    """VERIFIED means DNS is complete and only a mailbox is missing."""
    await _domain(session, DomainStatus.VERIFIED)
    step = await next_step(session, BASE)
    assert "sender identity" in step.title.lower()
    assert step.command and "mailboxes provision" in step.command


async def test_ready_domain_without_a_project_asks_for_one(session):
    d = await _domain(session, DomainStatus.READY)
    await _mailbox(session, d)
    step = await next_step(session, BASE)
    assert "project" in step.title.lower()
    assert step.href == "/keys"


async def test_project_without_a_key_asks_for_a_key(session):
    d = await _domain(session, DomainStatus.READY)
    await _mailbox(session, d)
    session.add(Project(name="app", active=True))
    await session.flush()
    step = await next_step(session, BASE)
    assert "key" in step.title.lower()
    assert step.href == "/keys"


async def _fully_configured(session) -> tuple[Domain, Mailbox, Project]:
    d = await _domain(session, DomainStatus.READY)
    m = await _mailbox(session, d)
    project = Project(name="app", active=True)
    session.add(project)
    await session.flush()
    session.add(ApiKey(project_id=project.id, name="k", key_hash="h", key_prefix="em_live_abc123"))
    await session.flush()
    return d, m, project


async def test_a_configured_installation_is_asked_to_prove_it_works(session):
    """Configured is not the same claim as working, and only one of the two can
    be demonstrated. Until a message has actually been through the pipeline the
    honest next step is to send one -- not to declare success."""
    await _fully_configured(session)

    step = await next_step(session, BASE)
    assert not step.done
    assert step.href == "/test"


async def test_a_proven_installation_says_it_is_ready(session):
    """Once a message exists, the pipeline has been exercised and the next
    useful thing really is the integration brief."""
    _, mailbox, project = await _fully_configured(session)
    session.add(
        Message(
            public_id="email_01TESTTESTTESTTESTTESTTEST",
            project_id=project.id,
            mailbox_id=mailbox.id,
            from_address=mailbox.address,
            to_addresses=["someone@example.net"],
        )
    )
    await session.flush()

    step = await next_step(session, BASE)
    assert step.done
    assert BASE in step.why
    assert step.href == "/integrate"


async def test_a_revoked_key_does_not_count_as_having_one(session):
    """Otherwise an installation whose only key was revoked is told it is ready
    to send, with nothing that can authenticate."""
    from datetime import UTC, datetime

    d = await _domain(session, DomainStatus.READY)
    await _mailbox(session, d)
    project = Project(name="app", active=True)
    session.add(project)
    await session.flush()
    session.add(
        ApiKey(
            project_id=project.id,
            name="k",
            key_hash="h",
            key_prefix="em_live_abc123",
            active=False,
            revoked_at=datetime.now(UTC),
        )
    )
    await session.flush()

    step = await next_step(session, BASE)
    assert not step.done
    assert "key" in step.title.lower()


@pytest.mark.parametrize(
    "status",
    [DomainStatus.ADDED, DomainStatus.DNS_INCOMPLETE],
)
async def test_domain_steps_are_done_in_the_browser(session, status):
    """Domain work is queued through the dashboard now, so these link rather
    than hand over a command."""
    await _domain(session, status)
    step = await next_step(session, BASE)
    assert step.href == "/domains"


async def test_mailbox_provisioning_still_carries_a_command(session):
    """The one step that genuinely cannot happen here: provisioning needs the
    MXRoute credential AND the encryption key, and it can breach the provider's
    acceptable-use policy, which is a judgement call for a person. Saying "do
    it elsewhere" without saying how is where the friction was."""
    await _domain(session, DomainStatus.VERIFIED)
    step = await next_step(session, BASE)
    # "./appctl", not "appctl": the binary is not on PATH, and a pasted bare
    # `appctl` answers "command not found" -- which reads as a broken install
    # rather than a wrong prefix. The docs have always used ./appctl.
    assert step.command and step.command.startswith("./appctl admin mailboxes provision")


# --- guidance for a domain added to a WORKING installation -----------------
#
# The failure these exist for: every branch of next_step used to be guarded on
# `not ready`, so the guidance switched itself off permanently once one domain
# started sending. The operator who added a second domain months later saw
# "Ready to send" on the overview while the new domain sat at `verified`, and
# had nothing anywhere naming the step that would finish it.
#
# That is the case that matters more, not less: on the first run you are
# following instructions; by the second you have forgotten the vocabulary.


async def _ready_and_sending(session) -> Domain:
    """A complete, working installation -- the state that used to silence
    every remaining hint."""
    d = await _domain(session, DomainStatus.READY)
    m = await _mailbox(session, d)
    project = Project(name="app", active=True)
    session.add(project)
    await session.flush()
    session.add(ApiKey(project_id=project.id, name="k", key_hash="h", key_prefix="em_live_abc123"))
    session.add(
        Message(
            public_id="email_01WORKINGWORKINGWORKINGWO",
            project_id=project.id,
            mailbox_id=m.id,
            from_address=m.address,
            to_addresses=["someone@example.net"],
        )
    )
    await session.flush()
    return d


async def _second_domain(session, status: DomainStatus) -> Domain:
    d = Domain(name="newdomain.com", status=status)
    session.add(d)
    await session.flush()
    return d


async def test_a_verified_second_domain_is_not_hidden_by_a_working_first(session):
    """The exact reported failure."""
    await _ready_and_sending(session)
    await _second_domain(session, DomainStatus.VERIFIED)

    step = await next_step(session, BASE)
    assert not step.done
    assert "newdomain.com" in step.title
    assert step.command == "./appctl admin mailboxes provision noreply@newdomain.com"


async def test_an_unpublished_second_domain_is_not_hidden_either(session):
    await _ready_and_sending(session)
    await _second_domain(session, DomainStatus.DNS_INCOMPLETE)

    step = await next_step(session, BASE)
    assert "newdomain.com" in step.title
    assert step.href == "/domains"


async def test_a_working_installation_with_nothing_pending_still_says_ready(session):
    """The fix must not turn the panel into a permanent nag."""
    await _ready_and_sending(session)
    step = await next_step(session, BASE)
    assert step.done


# --- the per-domain decision the domains page renders ----------------------


async def test_a_ready_domain_needs_nothing(session):
    d = await _domain(session, DomainStatus.READY)
    assert domain_next_action(d, has_mailbox=True) is None


async def test_a_verified_domain_without_a_mailbox_carries_the_command(session):
    """The one step in the whole flow that cannot be done in the browser, so it
    is the one that most needs the exact command rather than a concept."""
    d = await _domain(session, DomainStatus.VERIFIED)
    action = domain_next_action(d, has_mailbox=False)
    assert action is not None
    assert action.command == "./appctl admin mailboxes provision noreply@example.com"
    assert "cannot be done here" in action.why


async def test_a_verified_domain_that_has_a_mailbox_asks_for_a_recheck(session):
    d = await _domain(session, DomainStatus.VERIFIED)
    action = domain_next_action(d, has_mailbox=True)
    assert action is not None
    assert "Re-check" in action.title


async def test_a_misconfigured_domain_says_dns_changed_outside_emaild(session):
    d = await _domain(session, DomainStatus.MISCONFIGURED)
    action = domain_next_action(d, has_mailbox=True)
    assert action is not None
    assert "outside emaild" in action.why


async def test_domain_actions_keys_by_name_and_omits_ready_ones(session):
    ready = await _domain(session, DomainStatus.READY)
    await _mailbox(session, ready)
    await _second_domain(session, DomainStatus.VERIFIED)

    actions = await domain_actions(session)
    assert set(actions) == {"newdomain.com"}
