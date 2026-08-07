"""Delivery adapter interface and MIME assembly.

The worker talks only to this interface. It never learns what SMTP is, which is
what makes swapping in SES later a configuration change rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Protocol


@dataclass
class OutboundMessage:
    """Everything needed to put one message on the wire.

    `envelope_from` is separate from `from_address` on principle, even though
    MXRoute forces them equal -- a provider that permits VERP would use the
    distinction, and collapsing it here would bake the limitation into the
    interface rather than the adapter.
    """

    public_id: str
    envelope_from: str
    from_address: str
    from_name: str | None
    to: list[str]
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: str | None = None
    subject: str | None = None
    html: str | None = None
    text: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def envelope_recipients(self) -> list[str]:
        """Everyone the SMTP transaction addresses, Bcc included.

        Bcc appears here but never in the headers -- that separation is the
        entire meaning of Bcc, and getting it wrong discloses recipients to each
        other.
        """
        return [*self.to, *self.cc, *self.bcc]


@dataclass
class DeliveryResult:
    """What the provider actually said. Deliberately not interpreted here."""

    success: bool
    code: int | None = None
    response: str | None = None
    refused: dict[str, tuple[int, str]] = field(default_factory=dict)
    accepted_recipients: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.refused) and bool(self.accepted_recipients)


class DeliveryAdapter(Protocol):
    """What the worker requires of any provider."""

    name: str

    async def send(self, message: OutboundMessage, *, host: str) -> DeliveryResult: ...

    async def close(self) -> None: ...


def build_mime(message: OutboundMessage, message_id: str) -> EmailMessage:
    """Assemble the RFC 5322 message.

    `message_id` is supplied rather than generated, because it is the bounce
    attribution key: VERP is unavailable on this provider, so a DSN quoting the
    original Message-ID is the only thread back to the message that caused it
    (spike_results.md, Finding 1a).
    """
    mime = EmailMessage()

    # Display name is free-form; the address is not. MXRoute pins the envelope
    # to the login, and a From: header that disagrees breaks DMARC alignment
    # even with SPF and DKIM passing.
    mime["From"] = (
        formataddr((message.from_name, message.from_address))
        if message.from_name
        else message.from_address
    )
    mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    # Bcc is deliberately absent: it belongs in the envelope only.
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    if message.subject:
        mime["Subject"] = message.subject

    mime["Message-ID"] = message_id
    mime["Date"] = formatdate(localtime=False, usegmt=True)

    for key, value in message.headers.items():
        # Never let a caller-supplied header overwrite one we depend on for
        # identity, alignment, or bounce attribution.
        if key.lower() in {"from", "to", "cc", "bcc", "message-id", "date", "subject", "reply-to"}:
            continue
        mime[key] = value

    if message.text and message.html:
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
    elif message.html:
        # Still send multipart/alternative with a plaintext part: an HTML-only
        # message scores worse with spam filters, and some clients show nothing.
        mime.set_content(_html_to_text(message.html))
        mime.add_alternative(message.html, subtype="html")
    else:
        mime.set_content(message.text or "")

    return mime


def _html_to_text(html: str) -> str:
    """A crude plaintext fallback when the caller supplied only HTML.

    Deliberately minimal -- not a renderer. Callers who care about the plaintext
    part should send `text`, and the API accepts both precisely so they can.
    """
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()
