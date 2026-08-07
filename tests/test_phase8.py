"""Phase 8 dashboard tests.

Two things get the most attention, because both fail silently:

* **Escaping.** Subjects and recipient addresses are caller-controlled and go
  straight into HTML. An unescaped subject is stored XSS against the operator.
* **Leakage.** The dashboard must never render a message body or any secret.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.config import Settings
from emaild.dashboard.auth import check_dashboard_auth
from emaild.models import (
    ApiKey,
    Base,
    Domain,
    DomainStatus,
    Event,
    Mailbox,
    Message,
    MessageStatus,
    Project,
    Suppression,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

XSS = '<script>alert("pwned")</script>'
SECRET_BODY = "Your reset token is SUPERSECRETTOKEN123"


def _settings(**kw) -> Settings:
    base = {
        "_env_file": None,
        "role": "api",
        "database_url": TEST_DSN or "postgresql+asyncpg://x/y",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


# --- fail-closed configuration --------------------------------------------


def test_production_refuses_an_unauthenticated_dashboard():
    """The dashboard shows recipient addresses, subjects, and volume. Serving it
    open in production must be a stated decision, not an oversight."""
    with pytest.raises(Exception) as exc:
        _settings(env="production", dashboard_enabled=True)
    assert "unauthenticated dashboard" in str(exc.value)


def test_production_allows_it_with_a_token():
    s = _settings(env="production", dashboard_token="hunter2")
    assert s.dashboard_token == "hunter2"


def test_production_allows_it_when_proxy_auth_is_acknowledged():
    """Cloudflare Access in front is the intended production shape -- but it has
    to be said out loud, because we cannot detect it."""
    s = _settings(env="production", dashboard_behind_proxy_auth=True)
    assert s.dashboard_behind_proxy_auth is True


def test_production_allows_it_when_the_dashboard_is_off():
    s = _settings(env="production", dashboard_enabled=False)
    assert s.dashboard_enabled is False


def test_development_needs_no_gate():
    """Local development binds to localhost; a password there is friction with
    no attacker to stop."""
    assert _settings(env="development").dashboard_enabled is True


# --- basic auth ------------------------------------------------------------


class _Req:
    def __init__(self, auth: str | None = None) -> None:
        self.headers = {"authorization": auth} if auth else {}


def _basic(token: str, user: str = "admin") -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def test_no_token_configured_means_no_challenge():
    assert check_dashboard_auth(_Req(), _settings()) is None  # type: ignore[arg-type]


def test_correct_token_is_accepted():
    s = _settings(dashboard_token="hunter2")
    assert check_dashboard_auth(_Req(_basic("hunter2")), s) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "header",
    [None, "Bearer hunter2", "Basic", "Basic !!!notbase64!!!", _basic("wrong")],
)
def test_bad_credentials_are_challenged(header):
    s = _settings(dashboard_token="hunter2")
    result = check_dashboard_auth(_Req(header), s)  # type: ignore[arg-type]
    assert result is not None
    assert result.status_code == 401
    assert result.headers["WWW-Authenticate"].startswith("Basic")


# --- rendering -------------------------------------------------------------


@pytest.fixture
async def seeded():
    engine = create_async_engine(TEST_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        project = Project(name="billing")
        domain = Domain(
            name="example.com",
            status=DomainStatus.READY,
            smtp_host="smtp.example.com",
            dns_state={"checks": {"mx": {"result": "pass"}, "dkim": {"result": "fail"}}},
            dns_checked_at=datetime.now(UTC),
        )
        session.add_all([project, domain])
        await session.flush()
        mailbox = Mailbox(
            domain_id=domain.id, address="noreply@example.com", password_encrypted="x"
        )
        session.add(mailbox)
        session.add(Suppression(address="dead@example.net", reason="hard bounce"))
        session.add(
            ApiKey(project_id=project.id, name="k", key_hash="h" * 64, key_prefix="em_live_abc")
        )
        await session.flush()

        message = Message(
            public_id="email_01XSSTEST",
            project_id=project.id,
            mailbox_id=mailbox.id,
            from_address="noreply@example.com",
            to_addresses=[f"victim+{XSS}@example.net"],
            subject=XSS,
            body_html=f"<p>{SECRET_BODY}</p>",
            body_text=SECRET_BODY,
            recipient_count=1,
            status=MessageStatus.ACCEPTED_BY_PROVIDER,
        )
        session.add(message)
        await session.flush()
        session.add(
            Event(message_id=message.id, sequence=1, event_type="api.accepted", detail={"x": 1})
        )
        await session.commit()
    await engine.dispose()
    yield


@pytest.fixture
def client(seeded, monkeypatch):
    # conftest.py already pinned EMAILD_ENV_FILE=none before emaild was
    # imported, so nothing here can pick up the operator's real .env.
    monkeypatch.setenv("EMAILD_ROLE", "api")
    monkeypatch.setenv("EMAILD_DATABASE_URL", TEST_DSN or "")
    from emaild.config import get_settings

    get_settings.cache_clear()
    from emaild.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "path", ["/", "/domains", "/messages", "/keys", "/suppressions", "/messages/email_01XSSTEST"]
)
def test_every_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_subject_is_escaped_not_executed(client):
    """An unescaped subject is stored XSS against whoever opens the dashboard."""
    for path in ("/messages", "/messages/email_01XSSTEST"):
        body = client.get(path).text
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body


def test_recipient_addresses_are_escaped(client):
    body = client.get("/messages/email_01XSSTEST").text
    assert "<script>" not in body


def test_message_body_is_never_rendered(client):
    """It may already be purged, and re-serving reset tokens from a web page is
    exactly the archive vision.md refuses to build."""
    for path in ("/messages", "/messages/email_01XSSTEST"):
        assert SECRET_BODY not in client.get(path).text


def test_key_hashes_are_never_rendered(client):
    body = client.get("/keys").text
    assert "h" * 64 not in body
    assert "em_live_abc" in body  # the display prefix is fine


def test_domains_page_names_the_failing_checks(client):
    body = client.get("/domains").text
    assert "dkim" in body
    assert "example.com" in body


def test_message_search_filters(client):
    assert "email_01XSSTEST" in client.get("/messages?q=xsstest").text
    assert "email_01XSSTEST" not in client.get("/messages?q=nothingmatches").text


def test_unknown_message_redirects_rather_than_500(client):
    response = client.get("/messages/email_NOPE", follow_redirects=False)
    assert response.status_code == 303


def test_dashboard_does_not_shadow_the_api(client):
    """The dashboard mounts at '/', so its routes must not swallow /v1/*."""
    response = client.get("/v1/me")
    assert response.status_code == 401  # auth error, not an HTML page
    assert response.json()["error"]["type"] == "authentication_error"


def test_health_endpoints_still_work(client):
    assert client.get("/health/live").json()["status"] == "alive"
    assert client.get("/version").json()["application"] == "emaild"
