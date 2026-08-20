"""Dashboard routes.

Originally read-only, on the reasoning that a dashboard which can revoke keys
matters far more when compromised than one that can only answer questions. That
held right up until the first real user said the honest thing: a tool you have
to relearn every time is a tool you do not use. Issuing a key for a new product
is the single most frequent operation here, and routing it through a CLI meant
looking up the CLI first.

So mutations exist now, bounded by what this container is *able* to do rather
than by what seems prudent:

  here      projects, API keys, suppressions -- database only
  CLI only  domains and mailboxes -- these need the MXRoute account-root
            credential and the mailbox encryption key, and role=api holds
            neither (emaild/config.py). The api container does not mount the
            volume containing them, so this is enforced by Docker rather than
            by intent.

Which lands in the right place: the frequent, low-consequence operation is two
clicks, and the rare one that can delete mailboxes or breach the provider's
acceptable-use policy still requires deliberately reaching for another tool.

Every mutation is a POST, CSRF-checked (emaild/dashboard/csrf.py -- necessary
because HTTP Basic credentials are attached by the browser automatically), and
logged with actor="dashboard" so the audit trail says where it came from.
"""

from __future__ import annotations

import logging
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
from emaild.dashboard import csrf
from emaild.dashboard.auth import check_dashboard_auth
from emaild.dashboard.forms import many, one, parse_form, stash, take
from emaild.db import session_scope
from emaild.management import (
    ManagementError,
    create_api_key,
    create_project,
    revoke_api_key,
)
from emaild.metrics import build_overview
from emaild.models import (
    ApiKey,
    Domain,
    DomainStatus,
    Mailbox,
    Message,
    MessageStatus,
    Project,
)
from emaild.suppressions import (
    InvalidAddress,
    SuppressionSource,
    add_suppression,
    count_suppressions,
    list_suppressions,
    remove_suppression,
)

log = logging.getLogger(__name__)

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
    # `flash` is a one-shot payload left by a mutation that redirected here;
    # `csrf_token` goes into every form on the page.
    flash = take(request.query_params.get("f"))
    return {
        "request": request,
        "page": page,
        "version": __version__,
        "environment": settings.env.value,
        "csrf_token": csrf.issue_token(settings),
        "flash": flash,
    }


async def _mutation_guard(request: Request) -> Response | None:
    """Authentication and CSRF, in that order, for a state-changing request."""
    if (refused := _guard(request)) is not None:
        return refused
    return None


def _back(path: str, *, ok: str | None = None, error: str | None = None, **extra: Any) -> Response:
    """Post/Redirect/Get. 303 so the browser reissues as GET.

    Messages travel by one-shot handle rather than as query text: a plain
    ?msg= is attacker-supplied content rendered on an authenticated page, and
    while Jinja's autoescaping stops it becoming XSS, it would still let a
    crafted link display a convincing lie inside the operator's own dashboard.
    """
    payload: dict[str, Any] = {"ok": ok, "error": error, **extra}
    return RedirectResponse(f"{path}?f={stash(payload)}", status_code=303)


async def _reject_bad_csrf(
    request: Request, form: dict[str, list[str]], path: str
) -> Response | None:
    reason = csrf.verify(request, one(form, csrf._FIELD), get_settings())
    if reason is None:
        return None
    log.warning("dashboard: rejected a mutation on %s (%s)", path, reason)
    return _back(path, error=reason)


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

        projects = (
            (await session.execute(select(Project).where(Project.active).order_by(Project.name)))
            .scalars()
            .all()
        )
        # Only READY domains can send, so a key scoped to anything else would
        # authenticate and then fail at the first request. Offer what works.
        sendable = (
            (
                await session.execute(
                    select(Mailbox)
                    .join(Domain, Mailbox.domain_id == Domain.id)
                    .where(Mailbox.active, Domain.status == DomainStatus.READY)
                    .order_by(Mailbox.address)
                )
            )
            .scalars()
            .all()
        )
        project_names = [p.name for p in projects]
        sender_addresses = [m.address for m in sendable]

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
        request,
        "keys.html",
        {
            **_base(request, "keys"),
            "keys": view,
            "projects": project_names,
            "senders": sender_addresses,
        },
    )


@router.post("/keys/create")
async def keys_create(request: Request) -> Response:
    if (refused := await _mutation_guard(request)) is not None:
        return refused
    form = await parse_form(request)
    if (bad := await _reject_bad_csrf(request, form, "/keys")) is not None:
        return bad

    name = one(form, "name")
    project = one(form, "project")
    senders = many(form, "mailbox")

    async with session_scope() as session:
        try:
            _, plaintext, scoped = await create_api_key(
                session, name, project, senders, actor="dashboard"
            )
        except ManagementError as exc:
            return _back("/keys", error=str(exc))

    # The plaintext never touches the redirect URL -- see forms.stash.
    return _back(
        "/keys",
        ok=f"Created key '{name}'.",
        secret=plaintext,
        secret_label=f"Scoped to {', '.join(scoped)}",
    )


@router.post("/keys/revoke")
async def keys_revoke(request: Request) -> Response:
    if (refused := await _mutation_guard(request)) is not None:
        return refused
    form = await parse_form(request)
    if (bad := await _reject_bad_csrf(request, form, "/keys")) is not None:
        return bad

    name = one(form, "name")
    async with session_scope() as session:
        try:
            message = await revoke_api_key(session, name, actor="dashboard")
        except ManagementError as exc:
            return _back("/keys", error=str(exc))
    return _back("/keys", ok=message)


@router.post("/projects/create")
async def projects_create(request: Request) -> Response:
    if (refused := await _mutation_guard(request)) is not None:
        return refused
    form = await parse_form(request)
    if (bad := await _reject_bad_csrf(request, form, "/keys")) is not None:
        return bad

    name = one(form, "name")
    async with session_scope() as session:
        try:
            await create_project(session, name, one(form, "description"), actor="dashboard")
        except ManagementError as exc:
            return _back("/keys", error=str(exc))
    return _back("/keys", ok=f"Created project '{name}'.")


@router.get("/suppressions", response_class=HTMLResponse)
async def suppressions(request: Request) -> Response:
    if (refused := _guard(request)) is not None:
        return refused

    async with session_scope() as session:
        # No project_id: this is the OPERATOR view and deliberately shows every
        # entry, unlike GET /v1/suppressions which is scoped to the calling
        # project.
        rows = await list_suppressions(session, limit=200)
        total = await count_suppressions(session)
        project_names = {
            p.id: p.name for p in (await session.execute(select(Project))).scalars().all()
        }

    view = [
        {
            "address": s.address,
            "source": s.source.value,
            "reason": s.reason,
            "created": _fmt(s.created_at),
            # None for operator-added entries and for anything predating the
            # column -- rendered as a dash rather than guessed at.
            "project": project_names.get(s.project_id) if s.project_id else None,
        }
        for s in rows
    ]
    return templates.TemplateResponse(
        request,
        "suppressions.html",
        {**_base(request, "suppressions"), "suppressions": view, "total": total},
    )


@router.post("/suppressions/add")
async def suppressions_add(request: Request) -> Response:
    if (refused := await _mutation_guard(request)) is not None:
        return refused
    form = await parse_form(request)
    if (bad := await _reject_bad_csrf(request, form, "/suppressions")) is not None:
        return bad

    address = one(form, "address")
    async with session_scope() as session:
        try:
            _, created = await add_suppression(
                session,
                address,
                source=SuppressionSource.MANUAL,
                reason=one(form, "reason") or "added from the dashboard",
            )
        except InvalidAddress as exc:
            return _back("/suppressions", error=str(exc))

    log.info("suppression added from dashboard: %s (new=%s)", address, created)
    return _back(
        "/suppressions",
        ok=(
            f"Suppressed {address}."
            if created
            else f"{address} was already suppressed; nothing changed."
        ),
    )


@router.post("/suppressions/remove")
async def suppressions_remove(request: Request) -> Response:
    """Un-suppress. This resumes mail to an address something distrusted."""
    if (refused := await _mutation_guard(request)) is not None:
        return refused
    form = await parse_form(request)
    if (bad := await _reject_bad_csrf(request, form, "/suppressions")) is not None:
        return bad

    address = one(form, "address")
    async with session_scope() as session:
        removed = await remove_suppression(session, address)

    if not removed:
        return _back("/suppressions", error=f"{address} is not on the suppression list.")
    log.warning("suppression removed from dashboard: %s", address)
    return _back("/suppressions", ok=f"Resumed sending to {address}.")
