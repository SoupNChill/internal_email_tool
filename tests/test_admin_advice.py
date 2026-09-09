"""The guidance printed when `domains add` fails.

Regression cover for a real incident: adding a domain returned 401 because the
MXRoute credentials in .env were wrong, and the command answered

    Most often this means the ownership TXT record is not yet resolving.
    Run 'domains token' and publish it first.

which is confident, specific, and about the wrong thing. The operator stopped
and asked rather than spending an afternoon on DNS, but the advice was the
reason they had to.
"""

from __future__ import annotations

import pytest

from emaild.admin import domain_add_advice
from emaild.domains import add_failure_message
from emaild.providers.mxroute import (
    MXRouteAuthError,
    MXRouteConflict,
    MXRouteError,
    MXRouteUnavailable,
)


def test_auth_failure_does_not_blame_dns():
    advice = domain_add_advice(MXRouteAuthError("credentials rejected (401)"), "example.com")
    assert "not a DNS problem" in advice
    assert "TXT" not in advice
    assert "domains token" not in advice


def test_auth_failure_names_the_variables_to_check():
    advice = domain_add_advice(MXRouteAuthError("401"), "example.com")
    assert "EMAILD_MXROUTE_USERNAME" in advice
    assert "EMAILD_MXROUTE_SERVER" in advice


def test_auth_failure_warns_off_the_two_values_people_actually_get_wrong():
    """The username is not an email address, and the server is the mail host
    rather than the API host. Both were live guesses during a real install."""
    advice = domain_add_advice(MXRouteAuthError("401"), "example.com")
    assert "email address" in advice
    assert "api.mxroute.com" in advice


def test_conflict_says_the_domain_already_exists():
    advice = domain_add_advice(MXRouteConflict("409"), "example.com")
    assert "already exists" in advice
    assert "example.com" in advice
    assert "TXT" not in advice


def test_unclassified_errors_keep_the_ownership_hint():
    """It was reasonable advice -- it was just being given unconditionally."""
    for exc in (MXRouteError("boom"), MXRouteUnavailable("503")):
        advice = domain_add_advice(exc, "example.com")
        assert "TXT" in advice
        assert "domains token" in advice


def test_every_branch_produces_distinct_advice():
    """Guards against a future refactor collapsing these back into one message."""
    messages = {
        domain_add_advice(exc, "example.com")
        for exc in (MXRouteAuthError("401"), MXRouteConflict("409"), MXRouteError("boom"))
    }
    assert len(messages) == 3


# --- the same advice, reaching the dashboard -------------------------------
#
# `domain_add_advice` was wired to the CLI only. A domain added from the
# dashboard is queued and run by the provisioner, and the job recorded
# `str(exc)` -- so the operator saw MXRoute's own words verbatim:
#
#   BUSINESS_ERROR: Domain verification required. Please add a TXT record to
#   prove domain ownership before adding this domain. Use the panel at
#   panel.mxroute.com to see the specific DNS record required.
#
# Accurate, and it sends the operator to another product to look up a record
# this one can fetch. A real operator followed it, which is the point: advice
# gets acted on, so it had better be the best advice available.


class _FakeClient:
    """Stands in for MXRouteClient. `get_verification_record` reads the account
    ownership TXT record, and the provisioner already holds a live client."""

    def __init__(self, record=None, fail=False):
        self._record = record or {
            "type": "TXT",
            "name": "_da-verify-abc123",
            "value": "domain-verified",
        }
        self._fail = fail


@pytest.fixture(autouse=True)
def _patch_record(monkeypatch):
    async def fake(client):
        if getattr(client, "_fail", False):
            raise RuntimeError("provider unreachable")
        return client._record

    monkeypatch.setattr("emaild.domains.get_verification_record", fake)


async def test_a_failed_add_includes_the_record_to_publish():
    """The whole point: the operator should not have to visit panel.mxroute.com
    to read a value emaild can fetch."""
    message = await add_failure_message(
        _FakeClient(), MXRouteError("BUSINESS_ERROR: Domain verification required."), "new.com"
    )
    assert "_da-verify-abc123" in message
    assert "domain-verified" in message
    assert "TXT" in message


async def test_an_auth_failure_still_does_not_fetch_or_blame_dns():
    """Same rule as the CLI: a 401 is not a DNS problem, and appending an
    ownership record would reintroduce the original bug through a new door."""
    message = await add_failure_message(_FakeClient(), MXRouteAuthError("401"), "new.com")
    assert "not a DNS problem" in message
    assert "_da-verify-abc123" not in message


async def test_a_conflict_does_not_fetch_the_record_either():
    message = await add_failure_message(_FakeClient(), MXRouteConflict("409"), "new.com")
    assert "already exists" in message
    assert "_da-verify-abc123" not in message


async def test_the_advice_survives_a_failure_to_fetch_the_record():
    """An error raised while explaining an error only hides the first one."""
    message = await add_failure_message(
        _FakeClient(fail=True), MXRouteError("BUSINESS_ERROR"), "new.com"
    )
    assert "ownership TXT record" in message
