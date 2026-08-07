"""Dashboard routes. Read-only, without exception.

Every mutation lives in the admin CLI. A dashboard that can revoke keys or
un-suppress addresses is one whose compromise matters far more than one that can
only answer questions -- and the CLI is where those actions get a confirmation
prompt and a log line anyway.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import selectinload

from emaild import __version__
from emaild.config import get_settings
from emaild.dashboard.auth import check_dashboard_auth
from emaild.db import session_scope
from emaild.metrics import build_overview
from emaild.models import (
    ApiKey,
    Domain,
    Mailbox,
    Message,
    MessageStatus,
    Project,
)
from emaild.suppressions import count_suppressions, list_suppressions

router = APIRouter(include_in_schema=False)

# autoescape is on by default in Jinja2Templates. It is the reason this is not
# built from f-strings: subjects and recipient addresses are caller-controlled.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_STATUS_PILL = {
    MessageStatus.ACCEPTED_BY_PROVIDER.value: "ok",
    MessageStatus.PERMANENTLY_REJECTED.value: "bad",
    MessageStatus.TEMPORARILY_FAILED.value: "warn",
    MessageStatus.SENDING.value: "warn",
    MessageStatus.QUEUED.value: "",
    MessageStatus.CANCELED.value: "",
}


def _fmt(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M") if value else None


def _guard(request: Request) -> Response | None:
    settings = get_settings()
    if not settings.dashboard_enabled:
        return Response(status_code=404, content="Dashboard is disabled.", media_type="text/plain")
    return check_dashboard_auth(request, settings)


def _base(request: Request, page: str) -> dict[str, Any]:
    settings = get_settings()
    return {
        "request": request,
        "page": page,
        "version": __version__,
        "environment": settings.env.value,
    }


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> Response:
    if (refused := _guard(request)) is not None:
        return refused
    settings = get_settings()
    async with session_scope() as session:
        o = await build_overview(session, safety_margin=settings.rate_limit_safety_margin)
    # Overview and QueueHealth are dataclasses, so the template can read them
    # directly -- no view-model wrapper needed.
    return templates.TemplateResponse(
        request, "overview.html", {**_base(request, "overview"), "o": o}
    )


@router.get("/domains", response_class=HTMLResponse)
async def domains(request: Request) -> Response:
    if (refused := _guard(request)) is not None:
        return refused
    async with session_scope() as session:
        rows = (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()

    view = []
    for d in rows:
        checks: dict[str, str] = {}
        missing: list[str] = []
        if isinstance(d.dns_state, dict):
            for name, info in (d.dns_state.get("checks") or {}).items():
                result = str(info.get("result", "?"))
                checks[name] = result
                if result != "pass" and name != "dmarc":
                    missing.append(name)
        view.append(
            {
                "name": d.name,
                "status": d.status.value,
                "smtp_host": d.smtp_host,
                "checked": _fmt(d.dns_checked_at),
                "checks": checks,
                "missing": missing,
            }
        )
    return templates.TemplateResponse(
        request, "domains.html", {**_base(request, "domains"), "domains": view}
    )


@router.get("/messages", response_class=HTMLResponse)
async def messages(
    request: Request, q: str | None = None, status: str | None = None, review: int = 0
) -> Response:
    if (refused := _guard(request)) is not None:
        return refused

    async with session_scope() as session:
        stmt = select(Message).order_by(Message.created_at.desc())
        count_stmt = select(func.count(Message.id))

        if q:
            like = f"%{q.strip().lower()}%"
            # to_addresses is JSONB; cast to text so a substring match works
            # without unnesting it. Fine at this volume.
            condition = or_(
                Message.public_id.ilike(like),
                func.lower(Message.subject).like(like),
                func.lower(cast(Message.to_addresses, Text)).like(like),
            )
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        if status:
            stmt = stmt.where(Message.status == status)
            count_stmt = count_stmt.where(Message.status == status)
        if review:
            stmt = stmt.where(Message.needs_review)
            count_stmt = count_stmt.where(Message.needs_review)

        rows = (await session.execute(stmt.limit(100))).scalars().all()
        total = (await session.execute(count_stmt)).scalar_one()

    view = []
    for m in rows:
        to = list(m.to_addresses or [])
        view.append(
            {
                "public_id": m.public_id,
                "status": m.status.value,
                "pill": _STATUS_PILL.get(m.status.value, ""),
                "needs_review": m.needs_review,
                "from_address": m.from_address,
                "to_summary": to[0] + (f" +{len(to) - 1}" if len(to) > 1 else "") if to else "—",
                "subject": m.subject,
                "created": _fmt(m.created_at),
            }
        )
    return templates.TemplateResponse(
        request,
        "messages.html",
        {
            **_base(request, "messages"),
            "messages": view,
            "total": total,
            "q": q,
            "status": status,
            "review": bool(review),
            "statuses": [s.value for s in MessageStatus],
        },
    )


@router.get("/messages/{public_id}", response_class=HTMLResponse)
async def message_detail(request: Request, public_id: str) -> Response:
    if (refused := _guard(request)) is not None:
        return refused

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Message, Project)
                .join(Project, Message.project_id == Project.id)
                .where(Message.public_id == public_id)
                .options(selectinload(Message.events))
            )
        ).one_or_none()
        if row is None:
            return RedirectResponse("/messages", status_code=303)
        m, project = row

        domain = m.from_address.split("@", 1)[1]
        view = {
            "public_id": m.public_id,
            "status": m.status.value,
            "pill": _STATUS_PILL.get(m.status.value, ""),
            "needs_review": m.needs_review,
            "attempts": m.attempts,
            "from_display": f"{m.from_name} <{m.from_address}>" if m.from_name else m.from_address,
            "to_addresses": list(m.to_addresses or []),
            "cc_addresses": list(m.cc_addresses or []),
            "bcc_addresses": list(m.bcc_addresses or []),
            "subject": m.subject,
            "project": project.name,
            "message_id_header": f"<{m.public_id}@{domain}>",
            "created": _fmt(m.created_at),
            "completed": _fmt(m.completed_at),
            "provider_latency_ms": m.provider_latency_ms,
            "failure_class": m.failure_class.value if m.failure_class else None,
            "failure_code": m.failure_code,
            "provider_response": m.provider_response,
            "events": [
                {"type": e.event_type, "at": _fmt(e.occurred_at), "detail": e.detail}
                for e in sorted(m.events, key=lambda e: e.sequence)
            ],
        }
    return templates.TemplateResponse(
        request, "message.html", {**_base(request, "messages"), "m": view}
    )


@router.get("/keys", response_class=HTMLResponse)
async def keys(request: Request) -> Response:
    if (refused := _guard(request)) is not None:
        return refused

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ApiKey, Project)
                .join(Project, ApiKey.project_id == Project.id)
                .options(selectinload(ApiKey.scopes))
                .order_by(ApiKey.created_at.desc())
            )
        ).all()
        mailboxes = {
            m.id: m.address for m in (await session.execute(select(Mailbox))).scalars().all()
        }

    view = [
        {
            "name": k.name,
            "project": p.name,
            "key_prefix": k.key_prefix,
            "state": "revoked" if k.revoked_at else ("active" if k.active else "inactive"),
            "last_used": _fmt(k.last_used_at),
            "scopes": [mailboxes.get(s.mailbox_id, "?") for s in k.scopes],
        }
        for k, p in rows
    ]
    return templates.TemplateResponse(
        request, "keys.html", {**_base(request, "keys"), "keys": view}
    )


@router.get("/suppressions", response_class=HTMLResponse)
async def suppressions(request: Request) -> Response:
    if (refused := _guard(request)) is not None:
        return refused

    async with session_scope() as session:
        rows = await list_suppressions(session, limit=200)
        total = await count_suppressions(session)

    view = [
        {
            "address": s.address,
            "source": s.source.value,
            "reason": s.reason,
            "created": _fmt(s.created_at),
        }
        for s in rows
    ]
    return templates.TemplateResponse(
        request,
        "suppressions.html",
        {**_base(request, "suppressions"), "suppressions": view, "total": total},
    )
