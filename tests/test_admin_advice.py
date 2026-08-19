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

from emaild.admin import domain_add_advice
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
