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

Kept separate from routes.py because it is a decision, not a rendering, and a
decision is worth testing on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.models import ApiKey, Domain, DomainStatus, Mailbox, Project


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


async def next_step(session: AsyncSession, base_url: str) -> NextStep:
    """The single most useful thing to do right now."""
    domains = (await session.execute(select(Domain))).scalars().all()

    if not domains:
        return NextStep(
            title="Add a sending domain",
            why=(
                "A domain is the part after the @ in the address you send from. "
                "It has to be added on the server: registering one needs the "
                "MXRoute account credential, which this dashboard is deliberately "
                "never given."
            ),
            command="appctl admin domains add yourdomain.com",
        )

    ready = [d for d in domains if d.status is DomainStatus.READY]
    verified = [d for d in domains if d.status is DomainStatus.VERIFIED]
    unpublished = [
        d
        for d in domains
        if d.status
        in (DomainStatus.ADDED, DomainStatus.DNS_INCOMPLETE, DomainStatus.OWNERSHIP_PENDING)
    ]

    if not ready and unpublished:
        d = unpublished[0]
        return NextStep(
            title=f"Publish DNS records for {d.name}",
            why=(
                "Receiving servers check DNS to confirm you are allowed to send "
                "as this domain. Until those records resolve, mail would be "
                "rejected or land in spam, so emaild will not send at all."
            ),
            href="/domains",
            link_label="See the exact records",
            command=f"appctl admin domains verify {d.name}",
        )

    mailbox_count = (await session.execute(select(func.count(Mailbox.id)))).scalar_one()

    if not ready and verified:
        d = verified[0]
        if mailbox_count == 0:
            return NextStep(
                title=f"Create a sender identity on {d.name}",
                why=(
                    "DNS is complete. A sender identity is one real address like "
                    "noreply@" + d.name + " — it is an actual mailbox with its own "
                    "400-per-hour budget, and it must exist before anything can send. "
                    "Provisioning it needs the MXRoute credential, so it runs on the server."
                ),
                command=f"appctl admin mailboxes provision noreply@{d.name}",
            )
        # Mailboxes exist but the domain was verified before they did, and
        # nothing recomputed it. Provisioning promotes the domain now, so this
        # only appears on installations that predate that fix.
        return NextStep(
            title=f"Re-check {d.name}",
            why=(
                "The domain has a sender identity but is still marked verified "
                "rather than ready. One re-check promotes it."
            ),
            command=f"appctl admin domains verify {d.name}",
        )

    if mailbox_count == 0:
        return NextStep(
            title="Create a sender identity",
            why=(
                "A sender identity is one real address like noreply@yourdomain.com. "
                "It is an actual mailbox with its own 400-per-hour budget, and mail "
                "can only be sent from one that exists."
            ),
            command="appctl admin mailboxes provision noreply@yourdomain.com",
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
