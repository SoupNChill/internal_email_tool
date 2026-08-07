"""Failure classification, built from responses we actually observed.

Every pattern here was captured from a live MXRoute server (spike_results.md),
not invented. That matters: a taxonomy guessed in advance is a taxonomy that
mislabels real failures.

The rule that carries the most weight, and the one most likely to be got wrong
by anyone reading only the SMTP RFCs:

    A 5xx normally means permanent. On this provider, the rate-limit 5xx is the
    exception -- it MUST be retried, because over-limit is rejected rather than
    deferred and the message would otherwise be silently destroyed.

And the safety net beneath it: a 5xx we do not recognise is re-queued and
flagged for review, never dropped. We would rather deliver a message twice than
lose one, and we would rather a human look at an unfamiliar response than have
the system quietly decide it was fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from emaild.models import FailureClass


@dataclass(frozen=True)
class Verdict:
    failure_class: FailureClass
    retryable: bool
    needs_review: bool = False
    note: str | None = None


# Ordered. First match wins, so put specific patterns above general ones.
# `code` of None matches any code.
_RULES: tuple[tuple[int | None, re.Pattern[str], Verdict], ...] = (
    # --- observed verbatim on chocobo.mxrouting.net -------------------------
    (
        535,
        re.compile(r"incorrect authentication data", re.I),
        Verdict(
            FailureClass.AUTH_FAILURE,
            retryable=False,
            needs_review=True,
            note="stored SMTP credential is wrong; sending for this identity is broken "
            "until it is rotated",
        ),
    ),
    (
        550,
        re.compile(r"no such recipient here", re.I),
        Verdict(FailureClass.RECIPIENT_REJECTED, retryable=False),
    ),
    (
        550,
        re.compile(r"must match your login", re.I),
        Verdict(
            FailureClass.SENDER_MISMATCH,
            retryable=False,
            needs_review=True,
            note="envelope sender did not equal the SMTP login -- a bug on our side, "
            "not the recipient's",
        ),
    ),
    (
        550,
        re.compile(r"only accepted from authorized IP ranges", re.I),
        Verdict(FailureClass.SENDER_UNAUTHORIZED, retryable=False, needs_review=True),
    ),
    # --- rate limiting: 5xx that MUST retry ---------------------------------
    # Exact wording unconfirmed (it would take deliberately blowing the limit to
    # capture). These patterns are deliberately broad: a false positive costs a
    # retry, a false negative destroys a legitimate message.
    (
        None,
        re.compile(
            r"rate limit|too many messages|message limit|sending limit|quota exceeded"
            r"|exceeded .*limit|limit exceeded|try again later|throttl",
            re.I,
        ),
        Verdict(
            FailureClass.RATE_LIMITED,
            retryable=True,
            note="over the provider's hourly ceiling; our limiter should have "
            "prevented this reaching the wire",
        ),
    ),
    # --- generic recipient/policy rejections --------------------------------
    (
        None,
        re.compile(
            r"user unknown|no such user|mailbox unavailable|does not exist|invalid recipient", re.I
        ),
        Verdict(FailureClass.RECIPIENT_REJECTED, retryable=False),
    ),
    (
        None,
        re.compile(r"spam|blocked|blacklist|reputation|policy|rejected due to", re.I),
        Verdict(FailureClass.POLICY_REJECTED, retryable=False, needs_review=True),
    ),
    (
        None,
        re.compile(r"message too large|size exceeds|exceeds maximum", re.I),
        Verdict(FailureClass.MESSAGE_INVALID, retryable=False),
    ),
    (
        None,
        re.compile(r"greylist|greylisted|temporarily deferred|try again", re.I),
        Verdict(FailureClass.PROVIDER_DEFERRAL, retryable=True),
    ),
)


def classify(code: int | None, response: str | None) -> Verdict:
    """Turn an SMTP code and response line into a decision.

    `code` may be None for transport-level failures (DNS, TLS, connection
    refused), which are always retryable -- nothing about the message is wrong.
    """
    text = (response or "").strip()

    if code is None:
        return Verdict(
            FailureClass.CONNECTION,
            retryable=True,
            note="no SMTP response: transport failure, nothing wrong with the message",
        )

    for rule_code, pattern, verdict in _RULES:
        if rule_code is not None and rule_code != code:
            continue
        if pattern.search(text):
            return verdict

    if 200 <= code < 300:
        return Verdict(
            FailureClass.UNKNOWN, retryable=False, note="success misrouted to classify()"
        )

    if 400 <= code < 500:
        # 4xx is a deferral by definition. Retrying is what the code means.
        return Verdict(FailureClass.PROVIDER_DEFERRAL, retryable=True)

    if code >= 500:
        # The safety net. An unrecognised permanent-looking failure is retried
        # and flagged rather than dropped -- we do not let the system silently
        # decide a message was fatal on the strength of wording nobody has seen.
        return Verdict(
            FailureClass.UNKNOWN,
            retryable=True,
            needs_review=True,
            note=f"unrecognised {code} response; re-queued and flagged rather than discarded",
        )

    return Verdict(FailureClass.UNKNOWN, retryable=True, needs_review=True)
