"""Dashboard mutations, and the CSRF protection they made necessary.

The dashboard authenticates with HTTP Basic. Browsers attach those credentials
to every request to the origin, including a form posted by a page on another
site -- so the moment a POST route existed, any page the operator visited could
have created an API key on their emaild with no XSS and no stolen password.
These tests exist mostly to make sure that stays impossible.

The other half is the secret-handling rule: a created key is shown once and
never recoverable, so it must not survive in a URL, in history, or across a
refresh.
"""

from __future__ import annotations

import base64
import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.config import Settings
from emaild.dashboard import csrf, forms
from emaild.models import Base, Domain, DomainStatus, Mailbox, Project

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

TOKEN = "dashboard-test-token-value"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"x:{TOKEN}".encode()).decode()}


@pytest.fixture
async def seeded():
    engine = create_async_engine(TEST_DSN, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        domain = Domain(name="example.com", status=DomainStatus.READY)
        project = Project(name="billing", active=True)
        session.add_all([domain, project])
        await session.flush()
        session.add(
            Mailbox(
                domain_id=domain.id,
                address="noreply@example.com",
                password_encrypted="x",
                active=True,
            )
        )
        await session.commit()
    await engine.dispose()
    yield


@pytest.fixture
def client(seeded, monkeypatch):
    monkeypatch.setenv("EMAILD_ROLE", "api")
    monkeypatch.setenv("EMAILD_DATABASE_URL", TEST_DSN or "")
    monkeypatch.setenv("EMAILD_DASHBOARD_TOKEN", TOKEN)
    from emaild.config import get_settings

    get_settings.cache_clear()
    from emaild.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _token(client) -> str:
    """The CSRF token as a real browser would obtain it: off a rendered page."""
    from emaild.config import get_settings

    return csrf.issue_token(get_settings())


# Sentinel, not a value: "send whatever token the server would accept". A
# string default here reads to linters as a hardcoded credential.
_VALID = object()


def _post(client, path: str, data: dict, *, token: object = _VALID, **kw):
    """POST a form. `token=None` omits the CSRF field; a string sends it as-is."""
    body = dict(data)
    if token is _VALID:
        body["csrf_token"] = _token(client)
    elif token is not None:
        body["csrf_token"] = str(token)
    return client.post(path, data=body, headers={**AUTH, **kw.pop("headers", {})}, **kw)


# --- authentication --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/keys/create",
        "/keys/revoke",
        "/projects/create",
        "/suppressions/add",
        "/suppressions/remove",
    ],
)
def test_every_mutation_requires_authentication(client, path):
    """Unauthenticated, with a valid CSRF token, must still be refused."""
    response = client.post(path, data={"csrf_token": _token(client)}, follow_redirects=False)
    assert response.status_code == 401


# --- CSRF ------------------------------------------------------------------


def test_mutation_without_a_csrf_token_is_refused(client):
    before = client.get("/keys", headers=AUTH).text
    response = _post(
        client,
        "/keys/create",
        {"name": "forged", "project": "billing", "mailbox": "noreply@example.com"},
        token=None,
        follow_redirects=True,
    )
    assert "forged" not in response.text
    assert "forged" not in before


def test_mutation_with_a_wrong_csrf_token_is_refused(client):
    _post(
        client,
        "/projects/create",
        {"name": "forged-project"},
        token="not-the-right-token",
        follow_redirects=True,
    )
    assert "forged-project" not in client.get("/keys", headers=AUTH).text


def test_cross_site_origin_is_refused_even_with_a_valid_token(client):
    """The token is derived from a shared secret, so it is stable. If one ever
    leaks -- a screenshot, a shared terminal -- the Origin check is what is
    left."""
    _post(
        client,
        "/projects/create",
        {"name": "evil-project"},
        headers={"Origin": "https://attacker.example"},
        follow_redirects=True,
    )
    assert "evil-project" not in client.get("/keys", headers=AUTH).text


def test_same_origin_is_accepted(client):
    response = _post(
        client,
        "/projects/create",
        {"name": "same-origin-ok"},
        headers={"Origin": "http://testserver"},
        follow_redirects=True,
    )
    assert "same-origin-ok" in response.text


def test_absent_origin_is_allowed_when_the_token_is_valid(client):
    """curl and some privacy tools send no Origin at all. The token still gates
    it, so refusing here would break legitimate clients for no gain."""
    response = _post(client, "/projects/create", {"name": "no-origin"}, follow_redirects=True)
    assert "no-origin" in response.text


# --- key creation ----------------------------------------------------------


def test_creating_a_key_shows_the_plaintext_once(client):
    response = _post(
        client,
        "/keys/create",
        {"name": "app1", "project": "billing", "mailbox": "noreply@example.com"},
        follow_redirects=True,
    )
    assert "em_live_" in response.text
    assert "cannot be shown again" in response.text


def test_the_plaintext_key_never_appears_in_a_url(client):
    """It would land in browser history, proxy logs, and the Referer header
    sent to the next site visited."""
    response = _post(
        client,
        "/keys/create",
        {"name": "app2", "project": "billing", "mailbox": "noreply@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "em_live_" not in response.headers["location"]


def test_refreshing_after_creation_does_not_show_the_key_again(client):
    created = _post(
        client,
        "/keys/create",
        {"name": "app3", "project": "billing", "mailbox": "noreply@example.com"},
        follow_redirects=False,
    )
    location = created.headers["location"]
    first = client.get(location, headers=AUTH)
    second = client.get(location, headers=AUTH)

    # Match the FULL key, not the "em_live_" substring: the listing shows a
    # 14-character prefix for every key by design, so a substring check would
    # pass on the prefix and prove nothing about the secret.
    full_key = re.search(r"em_live_[A-Za-z0-9_\-]{30,}", first.text)
    assert full_key, "the plaintext key was not shown on first view"
    assert full_key.group(0) not in second.text


def test_a_key_with_no_sender_scope_is_refused(client):
    """It would authenticate and then be unable to send as anything, which
    reads as a broken key rather than an empty one."""
    response = _post(
        client,
        "/keys/create",
        {"name": "unscoped", "project": "billing"},
        follow_redirects=True,
    )
    assert "at least one sender identity" in response.text
    assert "em_live_" not in response.text


def test_duplicate_active_key_name_is_refused(client):
    payload = {"name": "dupe", "project": "billing", "mailbox": "noreply@example.com"}
    _post(client, "/keys/create", payload, follow_redirects=True)
    response = _post(client, "/keys/create", payload, follow_redirects=True)
    assert "already exists" in response.text


def test_revoking_then_reusing_the_name_is_allowed(client):
    """Revoke-and-recreate under the same name is the documented rotation, and
    is what a real operator did on the first production install."""
    payload = {"name": "rotating", "project": "billing", "mailbox": "noreply@example.com"}
    _post(client, "/keys/create", payload, follow_redirects=True)
    _post(client, "/keys/revoke", {"name": "rotating"}, follow_redirects=True)
    response = _post(client, "/keys/create", payload, follow_redirects=True)
    assert "em_live_" in response.text


# --- suppressions ----------------------------------------------------------


def test_suppressing_and_removing(client):
    added = _post(
        client, "/suppressions/add", {"address": "bad@example.net"}, follow_redirects=True
    )
    assert "bad@example.net" in added.text

    removed = _post(
        client, "/suppressions/remove", {"address": "bad@example.net"}, follow_redirects=True
    )
    assert "Resumed sending" in removed.text


def test_suppressing_an_invalid_address_is_refused(client):
    response = _post(
        client, "/suppressions/add", {"address": "not-an-address"}, follow_redirects=True
    )
    # The error names the offending input, so its presence proves nothing.
    # What matters is that no suppression was recorded.
    assert "not a valid email address" in response.text
    assert "Nothing suppressed" in response.text


# --- one-shot store --------------------------------------------------------


def test_one_shot_values_are_destroyed_on_read():
    handle = forms.stash({"ok": "hello"})
    assert forms.take(handle) == {"ok": "hello"}
    assert forms.take(handle) is None


def test_one_shot_take_of_an_unknown_handle_is_none():
    assert forms.take("nope") is None
    assert forms.take(None) is None


def test_csrf_token_differs_between_installations():
    """Derived from the dashboard token, so one install's token is useless
    against another."""
    a = Settings(
        _env_file=None, role="api", database_url="postgresql+asyncpg://x/y", dashboard_token="aaaa"
    )  # type: ignore[arg-type]
    b = Settings(
        _env_file=None, role="api", database_url="postgresql+asyncpg://x/y", dashboard_token="bbbb"
    )  # type: ignore[arg-type]
    assert csrf.issue_token(a) != csrf.issue_token(b)
