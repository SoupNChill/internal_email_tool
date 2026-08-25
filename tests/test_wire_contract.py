"""The HTTP contract a client actually codes against.

Everything here was tested at the function level and nothing at the wire, which
is the level an integrator sees. That gap produced a real error: the integration
brief told coding assistants the send endpoint answers 202, and it answers 200 --
the sort of thing that becomes `if (res.status === 202)` inside somebody's
signup flow.

The status code in particular is pinned deliberately. 202 Accepted is arguably
the more correct code for "durably queued, not yet sent", but Resend answers
200 and wire compatibility with Resend is the feature. Changing it would break
the assistants that compatibility exists to serve.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.crypto import generate_api_key
from emaild.models import (
    ApiKey,
    ApiKeyScope,
    Base,
    Domain,
    DomainStatus,
    Mailbox,
    Project,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

SENDER = "noreply@example.com"
_KEY: dict[str, str] = {}


@pytest.fixture
async def seeded():
    engine = create_async_engine(TEST_DSN, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        domain = Domain(
            name="example.com",
            status=DomainStatus.READY,
            dns_state={"checks": {"mx": {"result": "pass"}}},
        )
        project = Project(name="billing", active=True)
        session.add_all([domain, project])
        await session.flush()

        mailbox = Mailbox(domain_id=domain.id, address=SENDER, password_encrypted="x", active=True)
        session.add(mailbox)
        await session.flush()

        plaintext, digest, prefix = generate_api_key()
        key = ApiKey(project_id=project.id, name="k", key_hash=digest, key_prefix=prefix)
        session.add(key)
        await session.flush()
        session.add(ApiKeyScope(api_key_id=key.id, mailbox_id=mailbox.id))
        await session.commit()
        _KEY["value"] = plaintext
    await engine.dispose()
    yield


@pytest.fixture
def client(seeded, monkeypatch):
    monkeypatch.setenv("EMAILD_ROLE", "api")
    monkeypatch.setenv("EMAILD_DATABASE_URL", TEST_DSN or "")
    from emaild.config import get_settings

    get_settings.cache_clear()
    from emaild.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_KEY['value']}"}


def _body(**over) -> dict:
    payload = {
        "from": SENDER,
        "to": "customer@example.net",
        "subject": "Verify your email",
        "html": "<p>Click to verify.</p>",
        "text": "Click to verify.",
    }
    payload.update(over)
    return payload


# --- the contract ----------------------------------------------------------


def test_a_successful_send_answers_200(client):
    """Pinned because the brief claimed 202 and assistants code against it.
    Resend answers 200; matching it is the compatibility promise."""
    response = client.post("/v1/emails", json=_body(), headers=_auth())
    assert response.status_code == 200, response.text


def test_the_response_shape_is_id_and_status(client):
    body = client.post("/v1/emails", json=_body(), headers=_auth()).json()
    assert set(body) == {"id", "status"}
    assert body["id"].startswith("email_")
    assert body["status"] == "queued"


def test_the_sender_field_is_named_from_in_both_directions(client):
    """Python cannot use `from` as an identifier, so the model calls it
    `from_` internally. Both the request alias and the response alias have to
    hold, or every Resend-shaped client breaks at once.

    The model also accepts `from_` (populate_by_name is on, so internal code
    can build one by field name). That is deliberate and harmless: it is
    permissiveness in the direction that does not matter, since the promise is
    that Resend-shaped input works here, not that emaild-shaped input works
    against Resend.
    """
    created = client.post("/v1/emails", json=_body(), headers=_auth())
    assert created.status_code == 200

    view = client.get(f"/v1/emails/{created.json()['id']}", headers=_auth()).json()
    assert view["from"] == SENDER
    assert "from_" not in view
    assert "from_address" not in view


def test_a_single_recipient_may_be_a_bare_string(client):
    assert client.post("/v1/emails", json=_body(to="a@b.com"), headers=_auth()).status_code == 200


def test_recipients_may_be_a_list(client):
    response = client.post("/v1/emails", json=_body(to=["a@b.com", "c@d.com"]), headers=_auth())
    assert response.status_code == 200


def test_an_unknown_field_is_rejected_not_ignored(client):
    """A typo'd field name that is silently dropped is a message sent without
    the thing the caller asked for."""
    response = client.post("/v1/emails", json=_body(reply_too="x@y.com"), headers=_auth())
    assert response.status_code == 422


def test_sending_without_a_key_is_401(client):
    assert client.post("/v1/emails", json=_body()).status_code == 401


def test_sending_as_an_unscoped_address_is_refused(client):
    response = client.post(
        "/v1/emails", json=_body(**{"from": "other@example.com"}), headers=_auth()
    )
    assert response.status_code in (403, 422)
    assert "error" in response.json()


def test_errors_carry_a_type_and_message(client):
    """The documented error envelope. Clients branch on `type`."""
    body = client.post("/v1/emails", json=_body(**{"from": "nope@elsewhere.com"}), headers=_auth())
    error = body.json()["error"]
    assert error["type"]
    assert error["message"]


def test_the_same_idempotency_key_returns_the_same_id(client):
    headers = {**_auth(), "Idempotency-Key": "signup-42"}
    first = client.post("/v1/emails", json=_body(), headers=headers).json()
    second = client.post("/v1/emails", json=_body(), headers=headers).json()
    assert first["id"] == second["id"]


def test_the_same_idempotency_key_with_a_different_body_is_rejected(client):
    """A client bug worth surfacing loudly: it means two different messages
    were about to collapse into one."""
    headers = {**_auth(), "Idempotency-Key": "signup-43"}
    client.post("/v1/emails", json=_body(), headers=headers)
    response = client.post("/v1/emails", json=_body(subject="different"), headers=headers)
    assert response.status_code >= 400


def test_fetching_a_message_never_returns_its_body(client):
    """Verification links and reset tokens must not be re-servable from an API
    key. The brief promises this."""
    created = client.post("/v1/emails", json=_body(), headers=_auth()).json()
    view = client.get(f"/v1/emails/{created['id']}", headers=_auth()).json()
    assert "html" not in view
    assert "text" not in view
    assert "Click to verify" not in str(view)


def test_an_unknown_message_id_is_404(client):
    assert client.get("/v1/emails/email_01NOPE", headers=_auth()).status_code == 404
