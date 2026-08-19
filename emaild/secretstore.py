"""Secrets that generate themselves on first boot.

The problem this solves is friction, not cryptography.

Until now the mailbox encryption key had to exist in `.env` before the first
`docker compose up`, which meant a shell script had to run first to generate
it -- and that installer was the only reason a script existed at all. The
requirement bought nothing. The key is random either way; what actually matters
is that it is **stable across restarts** and that **role=api never holds it**.

A named Docker volume gives both. Each role mounts only the directory holding
the secrets it is allowed to have:

    api_secrets     -> dashboard_token            mounted by api
    worker_secrets  -> mailbox_encryption_key     mounted by worker, admin

So the API container cannot read the mailbox key for the same reason it could
not before -- it is not there. The scoping is enforced by the mount, which is a
stronger guarantee than the startup check in config.py, because it holds even
if that check is wrong.

WHAT THIS COSTS. The key is no longer visible in a file the operator edits, so
it is easier to forget that losing it is unrecoverable. Two things compensate:
generation logs a prominent warning exactly once, and `appctl key` prints it on
demand. See docs/backup-and-restore.md.

NOT A SECRETS MANAGER. This is a single-host tool. If you outgrow one host you
want the key in something built for it, and `EMAILD_MAILBOX_ENCRYPTION_KEY`
still takes precedence over the file precisely so that migration is possible
without changing anything here.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Mode 600. The container runs as uid 10001 (see Dockerfile), which owns the
# volume mountpoint because the directory exists in the image with that
# ownership -- Docker copies it to a fresh named volume.
_SECRET_MODE = 0o600


class SecretStoreError(Exception):
    """Raised when a secret cannot be read or created.

    Deliberately fatal. A worker that cannot obtain the encryption key must not
    start and quietly leave mail queued forever; it must say why.
    """


def read_or_create(directory: str | os.PathLike[str], name: str, factory: Callable[[], str]) -> str:
    """Return the secret `name` from `directory`, generating it if absent.

    Creation is atomic (O_CREAT|O_EXCL). Two containers starting at the same
    moment -- worker and admin both mount the same volume -- would otherwise
    race and one would overwrite the other's key, which for the mailbox key
    means silently losing the ability to decrypt every credential written in
    between. The loser of the race reads the winner's value instead.
    """
    path = Path(directory) / name

    existing = _read_if_present(path)
    if existing is not None:
        return existing

    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SecretStoreError(
            f"cannot create the secrets directory {directory}: {exc}. "
            "Is the volume mounted, and writable by uid 10001?"
        ) from exc

    value = factory()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _SECRET_MODE)
    except FileExistsError as exc:
        # Lost the race. The winner's value is authoritative -- never ours.
        existing = _read_if_present(path)
        if existing is None:
            raise SecretStoreError(f"{path} appeared but could not be read") from exc
        return existing
    except OSError as exc:
        raise SecretStoreError(f"cannot create {path}: {exc}") from exc

    with os.fdopen(fd, "w") as handle:
        handle.write(value)

    logger.warning(
        "generated a new %s in %s. If this is not a first install, something "
        "replaced the volume and previously stored data may no longer be readable.",
        name,
        directory,
    )
    return value


def _read_if_present(path: Path) -> str | None:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SecretStoreError(f"cannot read {path}: {exc}") from exc

    value = raw.strip()
    if not value:
        # An empty file is a half-finished write, not a valid secret. Refusing
        # is right: generating a replacement here would silently orphan every
        # credential encrypted with whatever used to be in it.
        raise SecretStoreError(
            f"{path} exists but is empty. Refusing to generate a replacement, "
            "because the original may still be needed to decrypt stored data. "
            "Restore it from your backup, or remove the file deliberately to "
            "start over."
        )

    _warn_if_readable_by_others(path)
    return value


def _warn_if_readable_by_others(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "%s is readable beyond its owner (mode %o). Tighten it to 600.",
            path,
            stat.S_IMODE(mode),
        )
