"""Secrets that generate themselves on first boot.

These cover the mechanism that replaced install.sh. The interesting cases are
not "does it make a key" but the ones where getting it wrong destroys data:
regenerating over an existing key, two containers racing to create one, and the
API acquiring a key it must never hold.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from emaild.config import Settings
from emaild.secretstore import SecretStoreError, read_or_create

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL", "postgresql+asyncpg://x/y")


def _counter():
    """A factory whose output changes every call.

    Lets a test distinguish "read the existing value" from "made a new one",
    which a fixed return value could not.
    """
    state = {"n": 0}

    def factory() -> str:
        state["n"] += 1
        return f"generated-{state['n']}"

    return factory


# ---------------------------------------------------------------------------
# read_or_create
# ---------------------------------------------------------------------------


def test_generates_when_absent(tmp_path: Path):
    value = read_or_create(tmp_path, "k", _counter())
    assert value == "generated-1"
    assert (tmp_path / "k").read_text() == "generated-1"


def test_creates_the_directory_if_it_does_not_exist(tmp_path: Path):
    nested = tmp_path / "does" / "not" / "exist"
    assert read_or_create(nested, "k", _counter()) == "generated-1"


def test_second_call_returns_the_first_value(tmp_path: Path):
    """The whole point. A key that changed on restart would orphan every
    credential encrypted with the previous one."""
    factory = _counter()
    first = read_or_create(tmp_path, "k", factory)
    second = read_or_create(tmp_path, "k", factory)
    assert first == second == "generated-1"


def test_written_mode_600(tmp_path: Path):
    read_or_create(tmp_path, "k", _counter())
    mode = stat.S_IMODE((tmp_path / "k").stat().st_mode)
    assert mode == 0o600, f"secret written world-readable: {mode:o}"


def test_surrounding_whitespace_is_stripped(tmp_path: Path):
    """A key edited by hand, or restored with a trailing newline, must compare
    equal to the one that was written -- otherwise decryption fails with a
    difference nobody can see."""
    (tmp_path / "k").write_text("  value-with-space\n")
    assert read_or_create(tmp_path, "k", _counter()) == "value-with-space"


def test_empty_file_refuses_rather_than_regenerating(tmp_path: Path):
    """The dangerous case. An interrupted write leaves a zero-byte file, and
    quietly generating a replacement would make every stored credential
    permanently unreadable while looking like a clean start."""
    (tmp_path / "k").write_text("")
    with pytest.raises(SecretStoreError, match="empty"):
        read_or_create(tmp_path, "k", _counter())


def test_does_not_overwrite_a_value_it_did_not_create(tmp_path: Path):
    (tmp_path / "k").write_text("supplied-by-operator")
    assert read_or_create(tmp_path, "k", _counter()) == "supplied-by-operator"


def test_loser_of_a_creation_race_adopts_the_winners_value(tmp_path: Path):
    """Worker and admin mount the same volume and can start simultaneously.
    Whoever loses the O_EXCL race must read the winner's key, never keep its
    own -- two keys in play means credentials that store cleanly and then fail
    to decrypt."""

    def factory_that_races() -> str:
        # Simulates the other container winning between our existence check
        # and our own O_CREAT|O_EXCL.
        (tmp_path / "k").write_text("winner")
        return "loser"

    assert read_or_create(tmp_path, "k", factory_that_races) == "winner"


# ---------------------------------------------------------------------------
# Settings integration
# ---------------------------------------------------------------------------


def _settings(**kw) -> Settings:
    base = {"_env_file": None, "database_url": TEST_DSN}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_worker_generates_a_usable_fernet_key(tmp_path: Path):
    settings = _settings(role="worker", secrets_dir=str(tmp_path))
    assert settings.mailbox_encryption_key is not None
    assert len(settings.mailbox_encryption_key) == 44

    # Must actually work as a Fernet key, not merely look like one.
    from emaild.crypto import MailboxCipher

    cipher = MailboxCipher(settings.mailbox_encryption_key)
    assert cipher.decrypt(cipher.encrypt("pw")) == "pw"


def test_worker_key_is_stable_across_restarts(tmp_path: Path):
    first = _settings(role="worker", secrets_dir=str(tmp_path)).mailbox_encryption_key
    second = _settings(role="worker", secrets_dir=str(tmp_path)).mailbox_encryption_key
    assert first == second


def test_admin_shares_the_workers_key(tmp_path: Path):
    """They mount the same volume in compose. Admin encrypts what the worker
    decrypts, so a mismatch here means mail that never sends."""
    worker = _settings(role="worker", secrets_dir=str(tmp_path)).mailbox_encryption_key
    admin = _settings(role="admin", secrets_dir=str(tmp_path)).mailbox_encryption_key
    assert worker == admin


def test_explicit_key_wins_over_the_generated_one(tmp_path: Path):
    """Precedence must favour the operator, so an existing installation that
    already has a key in .env is untouched by the upgrade, and so migrating to
    a real secrets manager later is possible."""
    supplied = "Bb1kMh0e_pJKtxJm5RXFF8pmvWZ_XYbFxK1hHYnQGTk="
    settings = _settings(role="worker", secrets_dir=str(tmp_path), mailbox_encryption_key=supplied)
    assert settings.mailbox_encryption_key == supplied
    assert not (tmp_path / "mailbox_encryption_key").exists()


def test_api_never_receives_a_mailbox_key_even_from_its_own_volume(tmp_path: Path):
    """Role scoping survives the change. In compose the API mounts a different
    volume, but even pointed at the worker's it must not adopt the key -- the
    startup rule in config.py rejects an API holding one at all."""
    read_or_create(tmp_path, "mailbox_encryption_key", lambda: "x" * 44)
    settings = _settings(role="api", secrets_dir=str(tmp_path))
    assert settings.mailbox_encryption_key is None


def test_api_generates_a_dashboard_token(tmp_path: Path):
    """This is what lets `docker compose up` satisfy the production dashboard
    rule with no operator input."""
    settings = _settings(role="api", env="production", secrets_dir=str(tmp_path))
    assert settings.dashboard_token
    assert len(settings.dashboard_token) >= 20


def test_production_api_starts_with_no_configuration_at_all(tmp_path: Path):
    """The end-to-end claim of the redesign: nothing supplied, still valid."""
    settings = _settings(role="api", env="production", secrets_dir=str(tmp_path))
    assert settings.dashboard_enabled
    assert settings.dashboard_token


def test_nothing_is_generated_without_a_secrets_dir():
    """Inert unless compose asks for it, so tests and host-side runs behave
    exactly as they did before this existed."""
    settings = _settings(role="api")
    assert settings.secrets_dir is None
    assert settings.dashboard_token is None


def test_a_corrupt_stored_key_is_rejected_not_replaced(tmp_path: Path):
    """Refusing is the safe move: the real key may still be needed to read
    what is already in the database."""
    (tmp_path / "mailbox_encryption_key").write_text("too-short")
    with pytest.raises(ValueError, match="44"):
        _settings(role="worker", secrets_dir=str(tmp_path))
