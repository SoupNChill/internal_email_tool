"""FastAPI application entrypoint (role=api).

This process is the internet-reachable one when running under the `public`
compose profile. It deliberately holds neither the MXRoute admin credential nor
the mailbox decryption key -- see deployment_and_release.md §3.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from emaild import __version__
from emaild.api.v1 import router as v1_router
from emaild.config import Role, get_settings
from emaild.db import dispose_engine, init_engine
from emaild.errors import ApiError, api_error_handler
from emaild.health import router as health_router
from emaild.logging_config import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.role is not Role.API:
        # Refuse rather than serve with the wrong authority. A worker's
        # credentials in an internet-facing process is exactly the failure this
        # separation exists to prevent.
        raise RuntimeError(f"emaild.main serves role=api but EMAILD_ROLE={settings.role.value}")

    init_engine(settings)
    log.info("emaild starting", extra={"context": settings.redacted()})

    yield

    await dispose_engine()
    log.info("emaild stopped")


app = FastAPI(
    title="emaild",
    version=__version__,
    description="Internal transactional email API.",
    lifespan=lifespan,
    # Docs stay on: this is an internal tool whose entire purpose is being easy
    # to integrate against. They expose no secrets.
    docs_url="/docs",
    redoc_url=None,
)

app.add_exception_handler(ApiError, api_error_handler)
app.include_router(health_router)
app.include_router(v1_router)
