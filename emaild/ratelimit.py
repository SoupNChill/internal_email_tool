"""Per-sender-identity rate gate.

This is a **gate, not a throttle**, and the distinction is the whole design.

MXRoute rejects an over-limit send with a permanent 5xx and does not queue it
(spec_sheet.md §4a). There is no provider-side recovery. So a message must never
be *attempted* while at the ceiling -- once the wire carries it, the outcome is
already decided.

Consequences that follow:

* We enforce 90% of the provider's 400/hour, so our backpressure engages before
  theirs does.
* Counting is conservative: an attempt counts the moment it begins, not when it
  succeeds. Counting only successes would let a burst of retries sail past the
  ceiling.
* The window is treated as ROLLING, which is the stricter of the two readings.
  Whether MXRoute uses a rolling window or fixed clock-hour buckets is still
  unmeasured (spike_results.md, open question #2); assuming rolling is safe under
  either, assuming fixed is not.

The count comes from `messages` rather than a maintained counter. That is exact,
self-healing, and impossible to drift -- and at a few hundred messages a day, an
indexed count is free. A separate counter would be faster and occasionally wrong,
which is the wrong trade when being wrong means destroying mail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.models import Mailbox, Message

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=1)


@dataclass
class Budget:
    mailbox_id: int
    used: int
    ceiling: int
    provider_ceiling: int

    @property
    def remaining(self) -> int:
        return max(0, self.ceiling - self.used)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def headroom_to_provider(self) -> int:
        """How many sends sit between our gate and the provider's hard wall."""
        return max(0, self.provider_ceiling - self.used)


async def current_budget(session: AsyncSession, mailbox: Mailbox, safety_margin: float) -> Budget:
    """How much of this identity's hourly allowance is already committed."""
    cutoff = datetime.now(UTC) - WINDOW
    used = (
        await session.execute(
            select(func.count(Message.id)).where(
                Message.mailbox_id == mailbox.id,
                Message.last_attempt_at.is_not(None),
                Message.last_attempt_at >= cutoff,
            )
        )
    ).scalar_one()

    provider_ceiling = mailbox.hourly_limit
    ceiling = max(1, int(provider_ceiling * safety_margin))
    return Budget(
        mailbox_id=mailbox.id, used=int(used), ceiling=ceiling, provider_ceiling=provider_ceiling
    )


async def next_window_opening(session: AsyncSession, mailbox_id: int) -> datetime:
    """When the oldest attempt in the window ages out, freeing one slot.

    Retrying earlier than this is guaranteed to hit the gate again, so the worker
    schedules against it instead of spinning.
    """
    cutoff = datetime.now(UTC) - WINDOW
    oldest = (
        await session.execute(
            select(func.min(Message.last_attempt_at)).where(
                Message.mailbox_id == mailbox_id,
                Message.last_attempt_at.is_not(None),
                Message.last_attempt_at >= cutoff,
            )
        )
    ).scalar_one()

    if oldest is None:
        return datetime.now(UTC)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    # Small cushion so a clock skew of milliseconds does not cause an immediate
    # second refusal.
    return oldest + WINDOW + timedelta(seconds=1)
