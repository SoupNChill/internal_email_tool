"""Executes queued domain jobs. The only long-running holder of account-root.

Exists so the dashboard can add and verify domains without holding the MXRoute
credential that does it. The API writes a row (emaild/jobs.py); this reads it.

What makes that safe is not this process being careful -- it is that the
request cannot express anything dangerous. JobType has two members, both about
domains, neither destructive. A completely compromised API can queue "add
example.com". There is no encoding of "delete every mailbox", so there is
nothing to reach for.

This process, by contrast, holds a credential that CAN delete mailboxes, so it
is built to be hard to reach:

  * no listener, no published port -- it only ever makes outbound calls
  * mounts the MXRoute credential but NOT the mailbox encryption key, because
    domain work never decrypts a password (see the note in emaild/config.py)
  * idles harmlessly when no credential is configured, instead of crash-looping
    or -- worse -- marking pending work failed for a reason that is about this
    installation rather than about the request

Graceful shutdown mirrors the worker: SIGTERM stops the loop, an in-flight job
finishes, and anything still claimed is recovered by the stale-claim reaper.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from emaild.config import Role, Settings, get_settings
from emaild.db import dispose_engine, init_engine, session_scope
from emaild.jobs import reap_stale, run_one
from emaild.logging_config import configure_logging
from emaild.providers.mxroute import MXRouteClient

log = logging.getLogger(__name__)

# Domain work is rare and never urgent -- an operator has just clicked a button
# and is watching a page. Fast enough to feel immediate, slow enough that an
# idle installation is not running four queries a second forever.
POLL_SECONDS = 3.0

# How often to sweep for jobs abandoned by a killed provisioner.
REAP_EVERY_SECONDS = 60.0


class Provisioner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stopping = asyncio.Event()
        self._warned_no_credentials = False

    def request_shutdown(self) -> None:
        log.info("provisioner: shutdown requested; finishing any in-flight job")
        self._stopping.set()

    def _credentials(self) -> tuple[str, str, str] | None:
        s = self._settings
        if s.mxroute_server and s.mxroute_username and s.mxroute_api_key:
            return s.mxroute_server, s.mxroute_username, s.mxroute_api_key
        return None

    async def _idle_without_credentials(self) -> None:
        """Wait, having said why exactly once.

        Deliberately does NOT fail the pending jobs. Nothing was attempted, and
        marking them failed would report a problem with this installation as
        though it were a problem with the request -- then the operator adds
        credentials, restarts, and the work they asked for is gone.
        """
        if not self._warned_no_credentials:
            log.warning(
                "provisioner: EMAILD_MXROUTE_* not configured, so queued domain "
                "jobs will wait. Add them to .env beside compose.yaml and "
                "restart; anything already queued then runs."
            )
            self._warned_no_credentials = True
        await self._sleep(POLL_SECONDS * 5)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on SIGTERM."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        log.info("provisioner: started")
        since_reap = 0.0

        while not self._stopping.is_set():
            credentials = self._credentials()
            if credentials is None:
                await self._idle_without_credentials()
                continue

            if self._warned_no_credentials:
                log.info("provisioner: credentials now present; resuming")
                self._warned_no_credentials = False

            try:
                if since_reap <= 0:
                    async with session_scope() as session:
                        await reap_stale(session)
                    since_reap = REAP_EVERY_SECONDS

                # A fresh client per batch rather than one held open for the
                # process lifetime: this loop is idle almost always, and a
                # connection parked for days against a provider we do not
                # control is a reconnect bug waiting to happen.
                did_work = False
                async with MXRouteClient(*credentials) as client:
                    while not self._stopping.is_set():
                        async with session_scope() as session:
                            if not await run_one(session, client):
                                break
                        did_work = True

                if not did_work:
                    await self._sleep(POLL_SECONDS)
                    since_reap -= POLL_SECONDS

            except Exception:  # noqa: BLE001 - the loop must outlive one bad cycle
                log.exception("provisioner: cycle failed; retrying")
                await self._sleep(POLL_SECONDS * 3)
                since_reap -= POLL_SECONDS * 3

        log.info("provisioner: stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.role is not Role.ADMIN:
        raise RuntimeError(
            f"emaild.provisioner serves role=admin but EMAILD_ROLE={settings.role.value}"
        )

    init_engine(settings)
    provisioner = Provisioner(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, provisioner.request_shutdown)

    try:
        await provisioner.run()
    finally:
        await dispose_engine()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
