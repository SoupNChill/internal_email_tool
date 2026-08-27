"""Send one test message from the dashboard.

Exists because the documented first-run sequence ended with a `curl` command.
Every step before it -- add a domain, read the DNS records, create a project,
create a key -- had been moved into the browser, and then the step that tells
you whether *any of it worked* still required assembling a bearer token, a JSON
body and a shell. That is the wrong step to leave in the terminal: it is the
one the operator reaches while they are least certain anything is configured
correctly, and the one they will want again months later when mail stops.

Three decisions worth stating.

**It goes through `ingest_message`, the production path.** A test send that used
a private shortcut would prove only that the shortcut works. This takes the same
route a real request takes -- sender authorization, recipient normalisation,
provider limits, suppression check, durable commit -- so a green result means
the pipeline is genuinely intact, and a red one fails in exactly the place a
real send would.

**Authorization comes from a real API key row, not from operator privilege.**
Key plaintexts are hashed and unrecoverable, so we cannot present one; instead
the `Principal` is rebuilt from the stored key and its scopes, which is what
`resolve_principal` produces after a successful bearer lookup. The scope check
in `authorize_sender` is therefore live: picking a sender the key is not scoped
to fails here exactly as it would in production. The alternative -- letting the
dashboard send as any mailbox because the operator is trusted -- would test a
configuration nobody runs.

**The content is fixed.** This is a diagnostic, not a compose window. A message
body the operator can edit invites using the dashboard as a mail client, which
is a different product with a different threat model, and it makes a failed test
ambiguous: was it the installation, or was it something about that message?
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from emaild import __version__
from emaild.api.schemas import SendEmailRequest
from emaild.auth import Principal
from emaild.errors import ApiError
from emaild.ingest import ingest_message
from emaild.models import ApiKey, Domain, DomainStatus, Mailbox

log = logging.getLogger(__name__)


class SendRefused(Exception):
    """A refusal with a message written for the operator, not a caller.

    Not named `TestSendError`: pytest collects any class beginning with `Test`,
    so importing it into a test module produced a collection warning on every
    run. A warning nobody can fix is a warning everybody learns to ignore.
    """


@dataclass(frozen=True)
class SendOption:
    """One (key, sender) pair the dashboard may offer.

    A pair rather than two independent dropdowns because the valid senders
    depend on which key is selected, and two free dropdowns can express a
    combination that does not exist. Rendering the pairs means the form cannot
    offer an invalid choice -- and `authorize_sender` still checks, because a
    form is a convenience and not a security boundary.
    """

    key_id: int
    key_name: str
    project_name: str
    sender: str

    @property
    def value(self) -> str:
        """Form value. `|` is safe: ids are integers and addresses cannot
        contain a pipe."""
        return f"{self.key_id}|{self.sender}"

    @property
    def label(self) -> str:
        return f"{self.sender} — key “{self.key_name}” ({self.project_name})"


async def send_options(session: AsyncSession) -> list[SendOption]:
    """Every (key, sender) pair that could actually send right now.

    Filtered to active keys on active projects, and to mailboxes on READY
    domains, because those are the only combinations that would not fail. An
    option that is certain to be refused is worse than a missing one: it invites
    the operator to debug their DNS from an error we could have predicted.
    """
    keys = (
        (
            await session.execute(
                select(ApiKey)
                .where(ApiKey.active, ApiKey.revoked_at.is_(None))
                .options(selectinload(ApiKey.scopes), selectinload(ApiKey.project))
                .order_by(ApiKey.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    sendable = {
        m.id: m.address
        for m in (
            (
                await session.execute(
                    select(Mailbox)
                    .join(Domain, Mailbox.domain_id == Domain.id)
                    .where(Mailbox.active, Domain.status == DomainStatus.READY)
                )
            )
            .scalars()
            .all()
        )
    }

    options: list[SendOption] = []
    for key in keys:
        if not key.project.active:
            continue
        for scope in key.scopes:
            address = sendable.get(scope.mailbox_id)
            if address is None:
                continue
            options.append(
                SendOption(
                    key_id=key.id,
                    key_name=key.name,
                    project_name=key.project.name,
                    sender=address,
                )
            )
    return sorted(options, key=lambda o: (o.sender, o.key_name))


def parse_option(raw: str) -> tuple[int, str]:
    """Split a submitted `key_id|sender` value. Never trusts the shape."""
    key_part, _, sender = raw.partition("|")
    if not key_part.isdigit() or not sender:
        raise SendRefused("Choose which address to send from.")
    return int(key_part), sender


async def _principal_for(session: AsyncSession, key_id: int) -> Principal:
    """Rebuild what a successful bearer authentication would have produced.

    Mirrors `resolve_principal` with one deliberate omission: it does NOT touch
    `last_used_at`. That column answers "is my application actually using this
    key?", and a dashboard test is not the application. Updating it here would
    make an unused key look live and quietly break the one signal that tells an
    operator a key is safe to revoke.
    """
    key = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.id == key_id)
            .options(selectinload(ApiKey.scopes), selectinload(ApiKey.project))
        )
    ).scalar_one_or_none()

    if key is None or key.revoked_at is not None or not key.active:
        raise SendRefused("That key no longer exists or has been revoked. Reload and try again.")
    if not key.project.active:
        raise SendRefused(f"The project “{key.project.name}” is inactive.")

    mailbox_ids = [s.mailbox_id for s in key.scopes]
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

    return Principal(
        api_key_id=key.id,
        key_name=key.name,
        project_id=key.project_id,
        project_name=key.project.name,
        mailboxes=mailboxes,
    )


def _content(sender: str, base_url: str) -> tuple[str, str, str]:
    """Subject, html, text.

    Written to be useful in the inbox rather than to look like a template. The
    timestamp distinguishes this attempt from the last one -- during a DNS
    problem an operator sends several, and "did that one arrive, or am I looking
    at the message from twenty minutes ago?" is a real way to misread a result.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"emaild test message — {stamp}"

    lines = [
        "This is a test message from your emaild installation.",
        "",
        f"Sent from:  {sender}",
        f"Sent at:    {stamp}",
        f"Dashboard:  {base_url}",
        f"Version:    {__version__}",
        "",
        "Receiving this confirms the whole path is working: DNS, the sender",
        "identity's SMTP credential, and delivery through MXRoute.",
        "",
        "Worth checking while you are here: open the message's original source",
        "(in Gmail, the three-dot menu, then 'Show original') and confirm SPF,",
        "DKIM and DMARC all say PASS. Mail that arrives with those failing will",
        "reach your inbox today and land in spam for other people tomorrow.",
    ]
    text = "\n".join(lines)

    # escape(), because `base_url` is derived from the Host header and this
    # string becomes the body of an outgoing email. Nothing here is rendered in
    # the dashboard, so Jinja's autoescaping never sees it -- a crafted Host on
    # a request that reaches the API could otherwise put markup into mail we
    # sign with our own DKIM key.
    rows = "".join(
        f'<tr><td style="padding-right:1rem;color:#666">{label}</td>'
        f"<td>{escape(value)}</td></tr>"
        for label, value in (
            ("Sent from", sender),
            ("Sent at", stamp),
            ("Dashboard", base_url),
            ("Version", __version__),
        )
    )
    html = (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        'font-size:15px;line-height:1.6;color:#111">'
        "<p>This is a test message from your emaild installation.</p>"
        '<table cellpadding="0" cellspacing="0" style="font-size:14px;margin:1rem 0">'
        f"{rows}"
        "</table>"
        "<p>Receiving this confirms the whole path is working: DNS, the sender "
        "identity's SMTP credential, and delivery through MXRoute.</p>"
        "<p>Worth checking while you are here: open this message's original "
        "source (in Gmail, the three-dot menu, then <em>Show original</em>) and "
        "confirm SPF, DKIM and DMARC all say PASS. Mail that arrives with those "
        "failing will reach your inbox today and land in spam for other people "
        "tomorrow.</p>"
        "</div>"
    )
    return subject, html, text


async def send_test_message(
    session: AsyncSession,
    *,
    option: str,
    recipient: str,
    base_url: str,
    body_retention_hours: int,
    idempotency_ttl_hours: int,
) -> str:
    """Queue one test message. Returns its public id.

    Raises SendRefused for anything the operator can fix from this page.
    """
    key_id, sender = parse_option(option)
    recipient = recipient.strip()
    if not recipient:
        raise SendRefused("Enter an address to send the test to.")

    principal = await _principal_for(session, key_id)
    subject, html, text = _content(sender, base_url)

    request = SendEmailRequest.model_validate(
        {
            "from": sender,
            "to": recipient,
            "subject": subject,
            "html": html,
            "text": text,
        }
    )

    try:
        result = await ingest_message(
            session,
            principal,
            request,
            # No idempotency key. Two clicks of a button labelled "send a test"
            # mean the operator wants two messages -- collapsing them would look
            # exactly like the second send silently failing.
            idempotency_key=None,
            body_retention_hours=body_retention_hours,
            idempotency_ttl_hours=idempotency_ttl_hours,
        )
    except ApiError as exc:
        # The API's own wording, which is already written for whoever is
        # debugging. Re-phrasing it here would give the same fault two different
        # explanations depending on where it was triggered.
        raise SendRefused(exc.message) from exc

    log.info("dashboard: queued test message %s from %s to a recipient", result.public_id, sender)
    return result.public_id
