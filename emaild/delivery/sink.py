"""Sink adapter -- test mode.

Records what would have been sent and returns success without opening a
connection. Cheap to build now because the interface already exists, and
awkward to retrofit later.

It matters more here than on a typical provider: MXRoute enforces a hard
400/hour ceiling per identity and a zero-tolerance acceptable-use policy with
account termination attached. Letting every developer integration burn real
quota against real reputation is an unnecessary risk when this costs almost
nothing.

It can also simulate failures, so the retry and classification paths get
exercised without needing a provider to misbehave on cue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from emaild.delivery.base import DeliveryResult, OutboundMessage, build_mime

log = logging.getLogger(__name__)


@dataclass
class SinkAdapter:
    """Accepts everything, sends nothing, remembers what it saw."""

    name: str = "sink"
    sent: list[dict[str, object]] = field(default_factory=list)

    # Optional failure injection, for exercising retry and classification.
    fail_with: tuple[int | None, str] | None = None
    fail_recipients: dict[str, tuple[int, str]] = field(default_factory=dict)

    async def send(self, message: OutboundMessage, *, host: str) -> DeliveryResult:
        # Build the MIME anyway: assembly bugs should surface in test mode, not
        # only once a real provider is involved.
        message_id = f"<{message.public_id}@{message.from_address.split('@', 1)[1]}>"
        mime = build_mime(message, message_id)

        self.sent.append(
            {
                "public_id": message.public_id,
                "host": host,
                "envelope_from": message.envelope_from,
                "recipients": message.envelope_recipients,
                "subject": message.subject,
                "size": len(mime.as_bytes()),
                "message_id": message_id,
            }
        )

        if self.fail_with is not None:
            code, text = self.fail_with
            return DeliveryResult(success=False, code=code, response=text)

        refused = {
            addr: self.fail_recipients[addr]
            for addr in message.envelope_recipients
            if addr in self.fail_recipients
        }
        accepted = [r for r in message.envelope_recipients if r not in refused]

        log.info("sink: accepted %s for %d recipient(s)", message.public_id, len(accepted))
        return DeliveryResult(
            success=bool(accepted),
            code=250 if accepted else 550,
            response="sink: accepted (nothing was actually sent)",
            refused=refused,
            accepted_recipients=accepted,
        )

    async def close(self) -> None:
        return None
