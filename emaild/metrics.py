"""Metrics.

vision.md is specific about what these are for: not analytics, but confidence.
A builder should be able to look at this and know whether email is healthy.

So the shape follows the questions it asks, and one principle governs the rest:

    **Queue age is the honest health signal.**

A heartbeat proves a loop is turning. Queue age proves work is actually moving,
and it catches a dead worker, a stuck rate gate, a provider outage, and an
exhausted send budget with one number. When only one thing can be watched, watch
that.

Everything here is deliberately computed on demand rather than maintained in
counters. At this volume the queries are trivial, and a counter that drifts is
worse than no counter -- it produces confident wrong answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.models import (
    ApiKey,
    Domain,
    Mailbox,
    Message,
    MessageStatus,
    Project,
    WorkerHeartbeat,
)

# A worker that has not reported in this long is presumed dead.
HEARTBEAT_STALE_AFTER = timedelta(minutes=2)

# Queue age past which something is wrong. Generous: a rate-gated message can
# legitimately wait, so this is set to catch stalls rather than backpressure.
QUEUE_AGE_WARNING = timedelta(minutes=15)

_PENDING = (MessageStatus.QUEUED, MessageStatus.TEMPORARILY_FAILED)


@dataclass
class QueueHealth:
    pending: int
    sending: int
    oldest_pending_seconds: float | None
    needs_review: int
    healthy: bool
    reason: str | None = None


@dataclass
class VolumeSlice:
    name: str
    requested: int
    accepted: int
    failed: int
    pending: int

    @property
    def failure_rate(self) -> float:
        settled = self.accepted + self.failed
        return round(self.failed / settled, 4) if settled else 0.0


@dataclass
class Overview:
    window_hours: int
    requested: int
    accepted: int
    failed: int
    pending: int
    failure_rate: float
    queue: QueueHealth
    latency_ms: dict[str, float | None]
    by_project: list[VolumeSlice] = field(default_factory=list)
    by_domain: list[VolumeSlice] = field(default_factory=list)
    failures_by_class: dict[str, int] = field(default_factory=dict)
    workers: list[dict[str, Any]] = field(default_factory=list)
    rate_headroom: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "totals": {
                "requested": self.requested,
                "accepted_by_provider": self.accepted,
                "failed": self.failed,
                "pending": self.pending,
                "failure_rate": self.failure_rate,
            },
            "queue": {
                "pending": self.queue.pending,
                "sending": self.queue.sending,
                "oldest_pending_seconds": self.queue.oldest_pending_seconds,
                "needs_review": self.queue.needs_review,
                "healthy": self.queue.healthy,
                "reason": self.queue.reason,
            },
            "provider_latency_ms": self.latency_ms,
            "by_project": [_slice_dict(s) for s in self.by_project],
            "by_domain": [_slice_dict(s) for s in self.by_domain],
            "failures_by_class": self.failures_by_class,
            "workers": self.workers,
            "rate_headroom": self.rate_headroom,
        }


def _slice_dict(s: VolumeSlice) -> dict[str, Any]:
    return {
        "name": s.name,
        "requested": s.requested,
        "accepted": s.accepted,
        "failed": s.failed,
        "pending": s.pending,
        "failure_rate": s.failure_rate,
    }


_ACCEPTED = Message.status == MessageStatus.ACCEPTED_BY_PROVIDER
_FAILED = Message.status == MessageStatus.PERMANENTLY_REJECTED
_PENDING_EXPR = Message.status.in_(_PENDING)


async def queue_health(session: AsyncSession) -> QueueHealth:
    """The one number worth watching.

    `oldest_pending_seconds` is measured from `created_at`, not from
    `next_attempt_at`: a message deferred with a long backoff is still a message
    the caller is waiting on, and hiding that behind "it is scheduled" would be
    the reassuring answer rather than the true one.
    """
    row = (
        await session.execute(
            select(
                func.count(case((_PENDING_EXPR, 1))),
                func.count(case((Message.status == MessageStatus.SENDING, 1))),
                func.min(case((_PENDING_EXPR, Message.created_at))),
                func.count(case((Message.needs_review, 1))),
            )
        )
    ).one()
    pending, sending, oldest, needs_review = row

    age_seconds: float | None = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        age_seconds = round((datetime.now(UTC) - oldest).total_seconds(), 1)

    healthy = True
    reason = None
    if age_seconds is not None and age_seconds > QUEUE_AGE_WARNING.total_seconds():
        healthy = False
        reason = (
            f"oldest pending message is {age_seconds / 60:.0f} minutes old; "
            "worker may be stopped, rate-gated, or the provider unreachable"
        )
    elif needs_review:
        healthy = False
        reason = f"{needs_review} message(s) flagged for human review"

    return QueueHealth(
        pending=int(pending or 0),
        sending=int(sending or 0),
        oldest_pending_seconds=age_seconds,
        needs_review=int(needs_review or 0),
        healthy=healthy,
        reason=reason,
    )


async def _volume_by(
    session: AsyncSession, label_column: Any, join, cutoff: datetime, project_id: int | None
) -> list[VolumeSlice]:
    stmt = (
        select(
            label_column,
            func.count(Message.id),
            func.count(case((_ACCEPTED, 1))),
            func.count(case((_FAILED, 1))),
            func.count(case((_PENDING_EXPR, 1))),
        )
        .select_from(Message)
        .where(Message.created_at >= cutoff)
        .group_by(label_column)
        .order_by(func.count(Message.id).desc())
    )
    for target, onclause in join:
        stmt = stmt.join(target, onclause)
    if project_id is not None:
        stmt = stmt.where(Message.project_id == project_id)

    return [
        VolumeSlice(name=str(name), requested=req, accepted=acc, failed=fail, pending=pend)
        for name, req, acc, fail, pend in (await session.execute(stmt)).all()
    ]


async def provider_latency(
    session: AsyncSession, cutoff: datetime, project_id: int | None
) -> dict[str, float | None]:
    """Percentiles, not a mean. One 30-second timeout would drag an average
    somewhere that describes no actual request."""
    stmt = select(
        func.percentile_cont(0.5).within_group(cast(Message.provider_latency_ms, Float)),
        func.percentile_cont(0.95).within_group(cast(Message.provider_latency_ms, Float)),
        func.max(Message.provider_latency_ms),
        func.count(Message.provider_latency_ms),
    ).where(Message.provider_latency_ms.is_not(None), Message.created_at >= cutoff)
    if project_id is not None:
        stmt = stmt.where(Message.project_id == project_id)

    p50, p95, worst, samples = (await session.execute(stmt)).one()
    return {
        "p50": round(float(p50), 1) if p50 is not None else None,
        "p95": round(float(p95), 1) if p95 is not None else None,
        "max": float(worst) if worst is not None else None,
        "samples": int(samples or 0),
    }


async def worker_status(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (await session.execute(select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_id)))
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    out = []
    for row in rows:
        seen = row.last_seen_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        age = (now - seen).total_seconds()
        out.append(
            {
                "worker_id": row.worker_id,
                "version": row.version,
                "last_seen_seconds_ago": round(age, 1),
                "messages_processed": row.messages_processed,
                "alive": age <= HEARTBEAT_STALE_AFTER.total_seconds(),
            }
        )
    return out


async def rate_headroom(
    session: AsyncSession, safety_margin: float, project_id: int | None
) -> list[dict[str, Any]]:
    """How close each sender identity is to the wall.

    Worth surfacing prominently: over-limit is a permanent rejection with no
    provider-side queue, so approaching the ceiling is the one form of
    backpressure that destroys mail rather than delaying it.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    stmt = (
        select(Mailbox.address, Mailbox.hourly_limit, func.count(Message.id))
        .select_from(Mailbox)
        .outerjoin(
            Message,
            and_(
                Message.mailbox_id == Mailbox.id,
                Message.last_attempt_at.is_not(None),
                Message.last_attempt_at >= cutoff,
            ),
        )
        .group_by(Mailbox.address, Mailbox.hourly_limit)
        .order_by(func.count(Message.id).desc())
    )
    if project_id is not None:
        stmt = stmt.where(or_(Message.project_id == project_id, Message.project_id.is_(None)))

    out = []
    for address, limit, used in (await session.execute(stmt)).all():
        ceiling = max(1, int(limit * safety_margin))
        out.append(
            {
                "sender": address,
                "used_this_hour": int(used or 0),
                "our_ceiling": ceiling,
                "provider_ceiling": limit,
                "remaining": max(0, ceiling - int(used or 0)),
                "utilisation": round(int(used or 0) / ceiling, 3) if ceiling else 0.0,
            }
        )
    return out


async def build_overview(
    session: AsyncSession,
    *,
    window_hours: int = 24,
    safety_margin: float = 0.9,
    project_id: int | None = None,
) -> Overview:
    """Everything at once. `project_id` scopes it; None is the operator view."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)

    totals_stmt = select(
        func.count(Message.id),
        func.count(case((_ACCEPTED, 1))),
        func.count(case((_FAILED, 1))),
        func.count(case((_PENDING_EXPR, 1))),
    ).where(Message.created_at >= cutoff)
    if project_id is not None:
        totals_stmt = totals_stmt.where(Message.project_id == project_id)
    requested, accepted, failed, pending = (await session.execute(totals_stmt)).one()

    failures_stmt = (
        select(Message.failure_class, func.count(Message.id))
        .where(Message.failure_class.is_not(None), Message.created_at >= cutoff)
        .group_by(Message.failure_class)
        .order_by(func.count(Message.id).desc())
    )
    if project_id is not None:
        failures_stmt = failures_stmt.where(Message.project_id == project_id)
    failures = {
        (fc.value if fc else "unknown"): int(count)
        for fc, count in (await session.execute(failures_stmt)).all()
    }

    settled = (accepted or 0) + (failed or 0)
    return Overview(
        window_hours=window_hours,
        requested=int(requested or 0),
        accepted=int(accepted or 0),
        failed=int(failed or 0),
        pending=int(pending or 0),
        failure_rate=round((failed or 0) / settled, 4) if settled else 0.0,
        queue=await queue_health(session),
        latency_ms=await provider_latency(session, cutoff, project_id),
        by_project=await _volume_by(
            session, Project.name, [(Project, Message.project_id == Project.id)], cutoff, project_id
        ),
        by_domain=await _volume_by(
            session,
            Domain.name,
            [(Mailbox, Message.mailbox_id == Mailbox.id), (Domain, Mailbox.domain_id == Domain.id)],
            cutoff,
            project_id,
        ),
        failures_by_class=failures,
        workers=await worker_status(session),
        rate_headroom=await rate_headroom(session, safety_margin, project_id),
    )


async def active_keys(session: AsyncSession) -> list[dict[str, Any]]:
    """ "Which API keys are active?" -- one of vision.md's questions."""
    rows = (
        await session.execute(
            select(ApiKey.name, Project.name, ApiKey.last_used_at, ApiKey.active, ApiKey.revoked_at)
            .join(Project, ApiKey.project_id == Project.id)
            .order_by(ApiKey.last_used_at.desc().nullslast())
        )
    ).all()
    return [
        {
            "key": name,
            "project": project,
            "last_used_at": last_used.isoformat() if last_used else None,
            "state": "revoked" if revoked else ("active" if active else "inactive"),
        }
        for name, project, last_used, active, revoked in rows
    ]
