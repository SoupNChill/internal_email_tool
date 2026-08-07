"""Deciding when a delivery failure is evidence an address is dead.

We have no asynchronous bounce feed. But we do get *some* per-recipient
rejections synchronously, at RCPT time, and those are real evidence -- the one
bounce signal available before bounce-mailbox processing exists.

The bar for acting on it is deliberately high, because a wrong auto-suppression
silently stops legitimate mail to a real person and nobody notices until they
complain. So:

* Only recipient-specific permanent failures count.
* Policy and reputation rejections never count -- they are about us, or about a
  transient state of the receiving server, not about whether the address exists.
* Our own bugs (sender mismatch, auth failure) never count. Suppressing a
  recipient because we misconfigured the sender would be absurd.
* Anything ambiguous is left alone. A missing suppression costs one wasted send;
  a wrong one costs a customer who never hears from us again.
"""

from __future__ import annotations

import re

from emaild.models import FailureClass

# Wording that names the recipient as nonexistent. Anything vaguer is not
# grounds for a permanent decision about someone's address.
_ADDRESS_IS_DEAD = re.compile(
    r"no such recipient|no such user|user unknown|unknown user"
    r"|mailbox (?:unavailable|not found|does not exist)"
    r"|recipient (?:address )?rejected|address (?:does not exist|not found)"
    r"|invalid recipient|does not exist",
    re.I,
)

# Classes that can, in principle, indicate a dead address.
_ELIGIBLE = frozenset({FailureClass.RECIPIENT_REJECTED})


def should_suppress(code: int | None, response: str | None, failure_class: FailureClass) -> bool:
    """True only when the response is direct evidence the address does not exist."""
    if failure_class not in _ELIGIBLE:
        return False
    if code is None or not (500 <= code < 600):
        # 4xx is a deferral: the address may be perfectly fine and the server
        # merely busy. Never permanent grounds.
        return False
    return bool(_ADDRESS_IS_DEAD.search(response or ""))


def suppression_reason(code: int | None, response: str | None) -> str:
    """A reason a human can act on months later, when reviewing the list."""
    trimmed = (response or "").strip()[:180]
    return f"auto: provider rejected recipient with {code} {trimmed}".rstrip()
