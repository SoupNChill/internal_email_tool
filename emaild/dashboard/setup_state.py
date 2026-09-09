"""What the operator should do next, decided from what actually exists.

Written after a first-time user got everything working and said the honest
thing: "I don't really see a clear 'this is what I should do', and I don't
understand what a key or a project is."

Both halves of that are the interface's fault. The dashboard showed five pages
of accurate state and never named an action, and it used vocabulary -- project,
key, sender identity -- that means something specific here and nothing obvious
anywhere else.

So this computes one next step, in the order the pieces actually depend on each
other, and each step carries the plain-language reason it exists. There is
exactly one, never a checklist: a list of five things to do is the same problem
as no guidance at all.

Steps that can be done here link to the page; steps that genuinely cannot --
provisioning a mailbox needs the MXRoute credential AND the encryption key --
carry the command instead. Saying "do this elsewhere" without saying how is
where the friction was.

Kept separate from routes.py because it is a decision, not a rendering, and a
decision is worth testing on its own.

Rewritten after the same operator got stuck a second time, months later, adding
a domain to a WORKING installation. Every branch below used to be guarded on
`not ready` -- meaning the guidance existed only until the first domain started
sending, and then went quiet forever. A half-finished second domain was
invisible to it, and the overview cheerfully said "Ready to send" while the new
domain sat at `verified` with nothing anywhere naming the next step.

That is the more important case, not the lesser one: on the first run the
operator is following instructions and paying attention, and by the second they
have forgotten the vocabulary and expect the tool to carry it. So the per-domain
decision is now its own function, it runs for every domain regardless of how
many others are working, and the domains page renders it inline -- on the page
the operator is already looking at when they get stuck.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.models import ApiKey, Domain, DomainStatus, Mailbox, Message, Project


@dataclass
class NextStep:
    """One action, and enough context to know why it is the one."""

    title: str
    why: str
    # A command to run on the server, when the step cannot be done here.
    command: str | None = None
    # Where in the dashboard to go, when it can.
    href: str | None = None
    link_label: str | None = None
    # True once nothing is left to do -- rendered as reassurance, not a task.
    done: bool = False


def domain_next_action(domain: Domain, *, has_mailbox: bool) -> NextStep | None:
    """What this ONE domain needs, or None when it needs nothing.

    Separate from `next_step` so the domains page can render it beside each
    domain. That placement is the point: an operator who knows they are working
    on domains goes to /domains, and the answer needs to be there rather than on
    a panel they would have to think to visit.
    """
    if domain.status is DomainStatus.READY:
        return None

    if domain.status is DomainStatus.SUSPENDED:
        return NextStep(
            title=f"{domain.name} is suspended",
            why=(
                "Suspension is set deliberately and the automatic DNS sweep will "
                "not clear it. Nothing will send from this domain until it is "
                "lifted."
            ),
        )

    if domain.status in (
        DomainStatus.ADDED,
        DomainStatus.DNS_INCOMPLETE,
        DomainStatus.OWNERSHIP_PENDING,
        DomainStatus.MISCONFIGURED,
    ):
        why = (
            "Receiving servers check DNS to confirm you may send as this domain. "
            "Until those records resolve, mail would be rejected or land in spam, "
            "so emaild will not send at all."
        )
        if domain.status is DomainStatus.MISCONFIGURED:
            why = (
                "This domain was working and its DNS no longer checks out, so "
                "something changed outside emaild. Compare the records below "
                "against your registrar."
            )
        return NextStep(
            title=f"Publish the DNS records for {domain.name}",
            why=why + " They are listed below; re-check once they resolve.",
        )

    # VERIFIED: DNS is complete and there is no mailbox to send from. This is
    # the step that stranded a real operator twice -- it is the only one in the
    # whole flow that cannot be done in the browser, because provisioning needs
    # both the MXRoute credential and the mailbox encryption key, and the api
    # container mounts neither.
    if not has_mailbox:
        return NextStep(
            title=f"Create a sender identity on {domain.name}",
            why=(
                "DNS is complete — this is the last step. A sender identity is "
                "one real address like noreply@" + domain.name + ", an actual "
                "mailbox with its own 400-per-hour budget, and mail can only be "
                "sent from one that exists. It is the one step that cannot be "
                "done here: creating it needs the MXRoute credential, which this "
                "container deliberately does not hold. Run this on the server, "
                "in the directory holding compose.yaml. The domain becomes "
                "ready by itself once it succeeds."
            ),
            command=f"./appctl admin mailboxes provision noreply@{domain.name}",
        )

    return NextStep(
        title=f"Re-check {domain.name}",
        why=(
            "The domain has a sender identity but is still marked verified "
            "rather than ready. One re-check promotes it."
        ),
        href="/domains",
        link_label="Re-check it",
    )


async def domain_actions(session: AsyncSession) -> dict[str, NextStep]:
    """`domain_next_action` for every tracked domain, keyed by domain name."""
    domains = (await session.execute(select(Domain))).scalars().all()
    with_mailbox = set(
        (await session.execute(select(Mailbox.domain_id).where(Mailbox.active))).scalars().all()
    )
    out: dict[str, NextStep] = {}
    for domain in domains:
        action = domain_next_action(domain, has_mailbox=domain.id in with_mailbox)
        if action is not None:
            out[domain.name] = action
    return out


async def next_step(session: AsyncSession, base_url: str) -> NextStep:
    """The single most useful thing to do right now."""
    domains = (await session.execute(select(Domain))).scalars().all()

    if not domains:
        return NextStep(
            title="Add a sending domain",
            why=(
                "A domain is the part after the @ in the address you send from. "
                "Adding it registers it with MXRoute and fetches the DNS records "
                "you need to publish."
            ),
            href="/domains",
            link_label="Add one",
        )

    # Any domain that still needs something, whether or not others are already
    # sending. The `not ready` guards this used to carry meant the guidance
    # switched itself off permanently the moment one domain worked.
    with_mailbox = set(
        (await session.execute(select(Mailbox.domain_id).where(Mailbox.active))).scalars().all()
    )
    for domain in sorted(domains, key=lambda d: d.name):
        action = domain_next_action(domain, has_mailbox=domain.id in with_mailbox)
        if action is not None:
            # Point at the page that shows the records and the re-check button,
            # unless the step is a command to run on the server.
            if action.command is None and action.href is None:
                action.href, action.link_label = "/domains", "Open domains"
            return action

    mailbox_count = (await session.execute(select(func.count(Mailbox.id)))).scalar_one()

    if mailbox_count == 0:
        return NextStep(
            title="Create a sender identity",
            why=(
                "A sender identity is one real address like noreply@yourdomain.com. "
                "It is an actual mailbox with its own 400-per-hour budget, and mail "
                "can only be sent from one that exists."
            ),
            command="./appctl admin mailboxes provision noreply@yourdomain.com",
        )

    project_count = (await session.execute(select(func.count(Project.id)))).scalar_one()
    if project_count == 0:
        return NextStep(
            title="Create a project",
            why=(
                "A project is one of your applications. Grouping keys under a "
                "project is what keeps one app from reading another app's mail "
                "history."
            ),
            href="/keys",
            link_label="Create one",
        )

    active_keys = (
        await session.execute(
            select(func.count(ApiKey.id)).where(ApiKey.active, ApiKey.revoked_at.is_(None))
        )
    ).scalar_one()
    if active_keys == 0:
        return NextStep(
            title="Create an API key",
            why=(
                "A key is the password your application uses to send. It is "
                "restricted to the sender addresses you tick, so a leaked key "
                "can send as those and nothing else."
            ),
            href="/keys",
            link_label="Create one",
        )

    # Everything is configured -- but "configured" and "working" are different
    # claims, and only one of them can be demonstrated. Until a message has
    # actually been through the pipeline, the honest next step is to send one.
    sent = (await session.execute(select(func.count(Message.id)))).scalar_one()
    if sent == 0:
        return NextStep(
            title="Send a test message",
            why=(
                "Everything is configured. Sending one message to your own inbox "
                "is what proves it: DNS, the sender identity's SMTP credential, "
                "and delivery through MXRoute are all exercised, and any of them "
                "being wrong is much easier to find now than from inside an "
                "application."
            ),
            href="/test",
            link_label="Send one",
        )

    return NextStep(
        title="Ready to send",
        why=(
            f"Point your application at {base_url} and give it a key. It is "
            "wire-compatible with Resend, so a coding assistant already knows "
            "the shape."
        ),
        href="/integrate",
        link_label="Get the integration brief",
        done=True,
    )
