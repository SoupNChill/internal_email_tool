"""Health and version endpoints.

Paths are fixed by release_rules §21 -- deployment tooling depends on their
semantics, so they are not changed casually:

    /health/live   process is up. No dependency checks. Never fails on a slow DB.
    /health/ready  safe to receive traffic: DB reachable, schema compatible.
    /version       what is actually running.

These are unauthenticated, so they must leak nothing (§17, §48): no DSNs, no
hostnames, no configuration values, no exception text from the database.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from emaild import BUILD_TIME, GIT_COMMIT, MAX_SCHEMA_VERSION, MIN_SCHEMA_VERSION, __version__
from emaild.config import get_settings
from emaild.db import session_scope

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# Bumped in lockstep with migrations. Compared against what the database
# actually reports so a mismatch is caught at startup, not at first query.
CURRENT_SCHEMA_VERSION = 2


@lru_cache(maxsize=1)
def _shipped_revisions() -> tuple[str | None, frozenset[str]]:
    """(head, all revisions) that THIS build ships, read from its own migrations.

    Counting rows in alembic_version cannot work: it always holds exactly one
    row. What matters is *which* revision, compared against what this build
    knows how to run.
    """
    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        return script.get_current_head(), frozenset(s.revision for s in script.walk_revisions())
    except Exception as exc:  # migrations not packaged alongside the app
        log.warning("cannot read shipped migrations: %s", exc)
        return None, frozenset()


async def _applied_revision() -> str | None:
    async with session_scope() as session:
        result = await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        return result.scalar_one_or_none()


def _classify_schema(applied: str | None, head: str | None, known: frozenset[str]) -> str:
    """release_rules §11: fail clearly rather than attempt permissive compatibility."""
    if applied is None:
        return "no_migrations_applied"
    if head is None:
        return "unverifiable"  # cannot prove compatibility either way
    if applied == head:
        return "ok"
    if applied not in known:
        # The database has been migrated by a NEWER build. Running an older
        # application against it risks writing data the new schema rejects.
        return "schema_ahead_of_application"
    return "migrations_pending"


@router.get("/health/live")
async def live() -> dict[str, str]:
    """Liveness. Deliberately checks nothing external.

    A liveness probe that fails when Postgres is slow causes an orchestrator to
    restart a perfectly healthy process, which converts a recoverable dependency
    blip into an outage.
    """
    return {"status": "alive"}


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, object]:
    """Readiness. Checks what must be true to serve traffic correctly."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        # Logged in full for the operator; the response says only "unavailable"
        # so an unauthenticated caller learns nothing about our topology.
        log.error("readiness: database unreachable: %s", exc)
        checks["database"] = "unavailable"
        checks["schema"] = "unknown"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": checks}

    try:
        head, known = _shipped_revisions()
        verdict = _classify_schema(await _applied_revision(), head, known)
        checks["schema"] = verdict
        # "unverifiable" does not block traffic: it means we could not read our
        # own migration files, which is a packaging problem, not evidence that
        # the database is wrong. Blocking on it would turn a diagnostic gap into
        # an outage.
        if verdict not in ("ok", "unverifiable"):
            healthy = False
    except Exception as exc:
        log.error("readiness: schema check failed: %s", exc)
        checks["schema"] = "unknown"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if healthy else "not_ready", "checks": checks}


@router.get("/version")
async def version() -> dict[str, object]:
    """Non-secret build information only (first_production_packaging §13).

    The installation ID is deliberately NOT exposed here -- this endpoint is
    unauthenticated, and installation identity belongs in operator diagnostics
    and the backup manifest, not on a public URL.
    """
    settings = get_settings()
    return {
        "application": "emaild",
        "version": __version__,
        "commit": GIT_COMMIT,
        "build_time": BUILD_TIME,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "supported_schema": {"min": MIN_SCHEMA_VERSION, "max": MAX_SCHEMA_VERSION},
        "role": settings.role.value,
        "environment": settings.env.value,
    }
