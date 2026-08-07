"""MXRoute SMTP adapter.

Port 465 with implicit TLS, per MXRoute's own client documentation. That choice
also removes the STARTTLS-stripping failure mode entirely, which is worth more
than the marginal familiarity of 587.

Provider limits are read from the EHLO banner rather than hardcoded:

    250-SIZE 52428800
    250-LIMITS MAILMAX=100 RCPTMAX=150

They arrive free on every connection and stay correct if MXRoute changes them.
Hardcoding would mean discovering a change as production failures.
"""

from __future__ import annotations

import logging
import re
import ssl
from dataclasses import dataclass

import aiosmtplib

from emaild.delivery.base import DeliveryResult, OutboundMessage, build_mime

log = logging.getLogger(__name__)

_LIMITS_RE = re.compile(r"MAILMAX=(\d+)|RCPTMAX=(\d+)", re.I)
_SIZE_RE = re.compile(r"^SIZE\s+(\d+)", re.I | re.M)


@dataclass
class ServerLimits:
    max_messages_per_connection: int
    max_recipients: int
    max_size_bytes: int

    @classmethod
    def from_ehlo(cls, banner: str, fallback: ServerLimits) -> ServerLimits:
        mailmax = rcptmax = None
        for match in _LIMITS_RE.finditer(banner):
            if match.group(1):
                mailmax = int(match.group(1))
            if match.group(2):
                rcptmax = int(match.group(2))
        size_match = _SIZE_RE.search(banner)
        return cls(
            max_messages_per_connection=mailmax or fallback.max_messages_per_connection,
            max_recipients=rcptmax or fallback.max_recipients,
            max_size_bytes=int(size_match.group(1)) if size_match else fallback.max_size_bytes,
        )


class SmtpAdapter:
    """Sends through MXRoute over authenticated SMTP.

    One connection per sender identity, reused up to the server's advertised
    MAILMAX. Reconnecting per message would be wasteful; ignoring MAILMAX would
    get the connection dropped mid-run.
    """

    name = "mxroute-smtp"

    def __init__(
        self,
        *,
        username: str,
        password: str,
        port: int = 465,
        timeout: float = 30.0,
        fallback_limits: ServerLimits | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._port = port
        self._timeout = timeout
        self._fallback = fallback_limits or ServerLimits(100, 150, 52_428_800)
        self._client: aiosmtplib.SMTP | None = None
        self._sent_on_connection = 0
        self._limits: ServerLimits | None = None

    @property
    def limits(self) -> ServerLimits:
        return self._limits or self._fallback

    async def _connect(self, host: str) -> aiosmtplib.SMTP:
        client = aiosmtplib.SMTP(
            hostname=host,
            port=self._port,
            use_tls=True,  # implicit TLS on 465
            timeout=self._timeout,
            tls_context=ssl.create_default_context(),
        )
        await client.connect()
        response = await client.ehlo()
        self._limits = ServerLimits.from_ehlo(response.message, self._fallback)
        await client.login(self._username, self._password)
        self._sent_on_connection = 0
        log.info(
            "smtp: connected to %s as %s (mailmax=%d rcptmax=%d size=%d)",
            host,
            self._username,
            self._limits.max_messages_per_connection,
            self._limits.max_recipients,
            self._limits.max_size_bytes,
        )
        return client

    async def _ensure_connection(self, host: str) -> aiosmtplib.SMTP:
        if self._client is not None:
            over_budget = self._sent_on_connection >= self.limits.max_messages_per_connection
            if over_budget or not self._client.is_connected:
                await self.close()
        if self._client is None:
            self._client = await self._connect(host)
        return self._client

    async def send(self, message: OutboundMessage, *, host: str) -> DeliveryResult:
        message_id = f"<{message.public_id}@{message.from_address.split('@', 1)[1]}>"
        mime = build_mime(message, message_id)

        try:
            client = await self._ensure_connection(host)
            errors, response = await client.send_message(
                mime,
                sender=message.envelope_from,
                recipients=message.envelope_recipients,
            )
            self._sent_on_connection += 1

            refused = {addr: (code, text) for addr, (code, text) in (errors or {}).items()}
            accepted = [r for r in message.envelope_recipients if r not in refused]
            return DeliveryResult(
                success=bool(accepted),
                code=250 if accepted else None,
                response=response or ("all recipients refused" if refused else None),
                refused=refused,
                accepted_recipients=accepted,
            )

        except aiosmtplib.SMTPRecipientsRefused as exc:
            # Every recipient rejected. The transaction reached the server, so
            # the per-recipient codes are the real information here.
            await self.close()
            # `.recipients` is a LIST of SMTPRecipientRefused, not a mapping --
            # each element carries its own address, code, and message.
            refused = {r.recipient: (r.code, r.message) for r in exc.recipients}
            first = next(iter(refused.values()), (550, "recipients refused"))
            return DeliveryResult(success=False, code=first[0], response=first[1], refused=refused)

        except aiosmtplib.SMTPResponseException as exc:
            # The server answered with a code. That answer is what gets
            # classified -- we do not second-guess it here.
            await self.close()
            return DeliveryResult(success=False, code=exc.code, response=exc.message)

        except (aiosmtplib.SMTPException, OSError, ssl.SSLError, TimeoutError) as exc:
            # No usable SMTP response: transport failure. code=None tells the
            # classifier this says nothing about the message itself.
            await self.close()
            return DeliveryResult(success=False, code=None, response=f"{type(exc).__name__}: {exc}")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.quit()
            except Exception as exc:  # noqa: BLE001 - closing must never raise
                # Suppressed deliberately: we are discarding this connection
                # either way, and a failure to say QUIT politely must never
                # propagate into the delivery path. Logged at debug so it is
                # still visible when chasing connection churn.
                log.debug("smtp: error during quit (connection discarded anyway): %s", exc)
            self._client = None
            self._sent_on_connection = 0
