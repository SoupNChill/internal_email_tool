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

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from emaild import MAX_SCHEMA_VERSION, MIN_SCHEMA_VERSION, __version__
from emaild.config import get_settings
from emaild.db import session_scope

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# Bumped in lockstep with migrations. Compared against what the database
# actually reports so a mismatch is caught at startup, not at first query.
CURRENT_SCHEMA_VERSION = 1


async def _schema_version() -> int | None:
    """Read the applied migration count. None when the DB is unreachable."""
    async with session_scope() as session:
        result = await session.execute(
            text("SELECT count(*) FROM alembic_version WHERE version_num IS NOT NULL")
        )
        row = result.scalar_one_or_none()
        return int(row) if row is not None else None


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
        applied = await _schema_version()
        if applied is None or applied == 0:
            checks["schema"] = "no_migrations_applied"
            healthy = False
        elif not (MIN_SCHEMA_VERSION <= CURRENT_SCHEMA_VERSION <= MAX_SCHEMA_VERSION):
            # §11: refuse permissive compatibility. Failing clearly is safer
            # than operating against a schema this build does not understand.
            checks["schema"] = "incompatible"
            healthy = False
        else:
            checks["schema"] = "ok"
    except Exception as exc:
        log.error("readiness: schema check failed: %s", exc)
        checks["schema"] = "unknown"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if healthy else "not_ready", "checks": checks}


@router.get("/version")
async def version() -> dict[str, object]:
    settings = get_settings()
    return {
        "application": "emaild",
        "version": __version__,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "supported_schema": {"min": MIN_SCHEMA_VERSION, "max": MAX_SCHEMA_VERSION},
        "role": settings.role.value,
        "environment": settings.env.value,
    }
