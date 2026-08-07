"""Credential generation and encryption at rest.

Two distinct concerns, deliberately kept apart:

* **API keys** are hashed with SHA-256 and never recoverable. They authenticate
  callers, so a one-way function is exactly right.
* **Mailbox passwords** are encrypted with Fernet and must be recoverable, because
  the worker has to present them to an SMTP server. Hashing them would make
  sending impossible.

Conflating the two is a common and expensive mistake in either direction.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string

from cryptography.fernet import Fernet, InvalidToken

# MXRoute requires 8+ characters with at least one upper, one lower, and one
# digit. We generate far longer than the minimum -- nobody types these.
_PASSWORD_LENGTH = 32

# Deliberately excludes shell metacharacters and quotes. These passwords travel
# through JSON, environment variables, and SMTP AUTH; an unlucky character in a
# generated secret is a debugging session nobody enjoys.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits

API_KEY_PREFIX = "em_live_"
_API_KEY_ENTROPY_BYTES = 32  # 256 bits


class DecryptionError(Exception):
    """Raised when a stored credential cannot be decrypted.

    Almost always means the encryption key changed. release_rules §44 forbids
    rotating it during ordinary upgrades precisely because this is the result.
    """


def generate_mailbox_password() -> str:
    """A password satisfying MXRoute's complexity rule, by construction.

    Rejection sampling rather than post-hoc patching: loop until the draw
    genuinely satisfies every class. With this alphabet and length the
    probability of needing more than one attempt is negligible, and the result
    keeps full entropy instead of having characters forced into positions.
    """
    while True:
        candidate = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, sha256_hash, display_prefix).

    The full key is shown to the operator exactly once and never stored. Only
    the hash is persisted, so a database disclosure does not yield usable keys.
    """
    token = secrets.token_urlsafe(_API_KEY_ENTROPY_BYTES).rstrip("=")
    full_key = f"{API_KEY_PREFIX}{token}"
    return full_key, hash_api_key(full_key), full_key[: len(API_KEY_PREFIX) + 6]


def hash_api_key(key: str) -> str:
    """SHA-256, not bcrypt/argon2 -- and that is the correct choice here.

    Slow hashes exist to frustrate brute force against low-entropy human
    passwords. These keys are 256 bits of CSPRNG output; a slow hash would add
    latency to every authenticated request while buying no security at all.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(presented: str, stored_hash: str) -> bool:
    """Constant-time comparison, so timing cannot reveal how much of a key matched."""
    return hmac.compare_digest(hash_api_key(presented), stored_hash)


class MailboxCipher:
    """Encrypts mailbox SMTP passwords at rest.

    Only role=worker and role=admin construct this; the public API has no access
    to the key and therefore cannot decrypt a credential even if compromised.
    """

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("utf-8"))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            # Deliberately says nothing about the ciphertext or the key.
            raise DecryptionError(
                "mailbox credential could not be decrypted -- the encryption key "
                "does not match the one used to store it"
            ) from exc
