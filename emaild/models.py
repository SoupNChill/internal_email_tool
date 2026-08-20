"""Database models.

Design notes worth keeping in view while reading:

* A mailbox IS a sender identity, one-to-one. MXRoute pins the SMTP envelope
  sender to the authenticated login exactly (spike_results.md, Finding 1), so
  `noreply@x.com` and `support@x.com` are two mailboxes, not one with options.

* `events` is append-only and is the product. Message rows mutate as work
  progresses; the event stream is what makes the timeline honest.

* Message bodies are stored because asynchronous delivery requires it, then
  purged after terminal state. Verification links and password-reset tokens must
  not become a permanent archive (vision.md).

* Statuses describe what we actually know. `ACCEPTED_BY_PROVIDER` is terminal
  for most messages and does NOT mean delivered -- bad external recipients are
  accepted at RCPT and bounce asynchronously (spike_results.md, Finding 2).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """A Postgres enum whose stored values are the members' VALUES, not their names.

    SQLAlchemy defaults to storing member names, which would put 'QUEUED' in the
    database while the API serialises 'queued'. Two spellings of one concept is
    how partial-index predicates and status filters silently stop matching.
    """
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [member.value for member in cls],
    )


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DomainStatus(str, enum.Enum):
    """Lifecycle from vision.md. Only READY may send."""

    ADDED = "added"
    OWNERSHIP_PENDING = "ownership_pending"
    DNS_INCOMPLETE = "dns_incomplete"
    VERIFIED = "verified"
    READY = "ready"
    SUSPENDED = "suspended"
    MISCONFIGURED = "misconfigured"


class MessageStatus(str, enum.Enum):
    QUEUED = "queued"
    SENDING = "sending"
    ACCEPTED_BY_PROVIDER = "accepted_by_provider"  # terminal; delivery UNKNOWN
    TEMPORARILY_FAILED = "temporarily_failed"  # will retry
    PERMANENTLY_REJECTED = "permanently_rejected"  # terminal
    CANCELED = "canceled"


TERMINAL_STATUSES = frozenset(
    {
        MessageStatus.ACCEPTED_BY_PROVIDER,
        MessageStatus.PERMANENTLY_REJECTED,
        MessageStatus.CANCELED,
    }
)


class FailureClass(str, enum.Enum):
    """Normalised failure taxonomy, seeded from observed responses.

    RATE_LIMITED is the important one: MXRoute answers over-limit with a
    permanent 5xx, but it is the single 5xx we must RETRY rather than treat as
    terminal. See spec_sheet.md §4a.
    """

    AUTH_FAILURE = "auth_failure"  # 535 Incorrect authentication data
    RECIPIENT_REJECTED = "recipient_rejected"  # 550 No such recipient here
    SENDER_UNAUTHORIZED = "sender_unauthorized"  # 550 not an authorized IP range
    SENDER_MISMATCH = "sender_mismatch"  # 550 envelope must match login
    RATE_LIMITED = "rate_limited"  # 5xx, but RETRYABLE
    PROVIDER_DEFERRAL = "provider_deferral"  # 4xx
    CONNECTION = "connection"  # network/TLS
    POLICY_REJECTED = "policy_rejected"
    MESSAGE_INVALID = "message_invalid"
    INTERNAL = "internal"
    UNKNOWN = "unknown"  # unmatched: re-queued and flagged, never dropped


class SuppressionSource(str, enum.Enum):
    MANUAL = "manual"
    BOUNCE = "bounce"
    COMPLAINT = "complaint"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Installation(Base):
    """Installation identity. Exactly one row, created once, never regenerated.

    Required by first_production_packaging §10. It must survive upgrades, backup,
    and restore, and it goes into the backup manifest so a restored backup can be
    matched to the installation it came from.

    Added in Phase 1 deliberately: generating this later would mean either a
    backfill against live installations or an identity that changes on upgrade,
    and §9 is explicit that generated identifiers are created once and preserved.
    Deliberately carries no hostname, IP, or personal information.
    """

    __tablename__ = "installation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    installed_at: Mapped[datetime] = _now_col()
    installed_version: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        # Belt and braces: the primary key is pinned to 1 so a second row is a
        # constraint violation rather than a silently ambiguous identity.
        CheckConstraint("id = 1", name="ck_installation_single_row"),
    )


class Project(Base):
    """A product that sends mail. Internal-only: created administratively."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    created_at: Mapped[datetime] = _now_col()

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="project")


class Domain(Base):
    """A sending domain.

    Domains are account-scoped rather than project-scoped, because that mirrors
    reality: MXRoute owns them at the account level. Access is granted through
    API key scopes, not ownership.
    """

    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    status: Mapped[DomainStatus] = mapped_column(
        _pg_enum(DomainStatus, "domain_status"),
        default=DomainStatus.ADDED,
        server_default=text("'added'"),
        nullable=False,
    )

    # Read from GET /domains/{d}/dns and persisted per domain. Never hardcoded:
    # the account is on chocobo today, but that is not a guarantee.
    smtp_host: Mapped[str | None] = mapped_column(String(253))

    # The exact records to publish, as returned by the provider and captured at
    # add/verify time. Persisted so the dashboard can show them: fetching them
    # live needs the MXRoute account-root credential, which role=api is never
    # given, and "what do I paste into my registrar" is the single most
    # asked-for thing on this screen.
    #
    # A snapshot, not a source of truth -- `domains verify` refreshes it. The
    # DKIM public key is the part that matters, and it is not secret: it is
    # published in DNS by definition.
    required_records: Mapped[list | None] = mapped_column(JSONB)

    # Cached DNS verification. Swept on a schedule, never checked on the send
    # path -- the control API allows only 100 reads/minute account-wide.
    dns_state: Mapped[dict | None] = mapped_column(JSONB)
    dns_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_token: Mapped[str | None] = mapped_column(String(120))

    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    mailboxes: Mapped[list[Mailbox]] = relationship(back_populates="domain")

    @property
    def can_send(self) -> bool:
        return self.status is DomainStatus.READY


class Mailbox(Base):
    """A sender identity. One address, one mailbox, one SMTP login.

    `address` is simultaneously the SMTP username, the envelope sender, and the
    required From: address -- MXRoute enforces all three being equal. Storing
    them as one column reflects that rather than inviting them to drift apart.
    """

    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="RESTRICT"), nullable=False
    )
    address: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)

    # Fernet-encrypted. Only role=worker and role=admin hold the key.
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    display_name: Mapped[str | None] = mapped_column(String(200))
    hourly_limit: Mapped[int] = mapped_column(
        Integer, default=400, server_default=text("400"), nullable=False
    )
    daily_limit: Mapped[int] = mapped_column(
        Integer, default=9600, server_default=text("9600"), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col()

    domain: Mapped[Domain] = relationship(back_populates="mailboxes")

    __table_args__ = (
        CheckConstraint("hourly_limit > 0 AND hourly_limit <= 400", name="ck_mailbox_hourly_limit"),
        CheckConstraint("daily_limit > 0 AND daily_limit <= 9600", name="ck_mailbox_daily_limit"),
        Index("ix_mailboxes_domain", "domain_id"),
    )


class ApiKey(Base):
    """A scoped credential issued to one project.

    Stored as SHA-256 of the key. That is correct rather than lazy: slow hashes
    (bcrypt/argon2) exist to frustrate brute force against low-entropy human
    passwords. These keys are 256 bits of CSPRNG output, so a slow hash would add
    latency to every request while buying nothing.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)  # display only

    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    created_at: Mapped[datetime] = _now_col()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="api_keys")
    scopes: Mapped[list[ApiKeyScope]] = relationship(
        back_populates="api_key", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_api_keys_hash_active", "key_hash", "active"),)


class ApiKeyScope(Base):
    """Which sender identities a key may use.

    Scoping is to mailboxes rather than domains, deliberately: adding a new
    mailbox to a domain then does NOT silently widen an existing key's authority.
    Domain-level granting is a UI convenience that inserts one row per mailbox.
    """

    __tablename__ = "api_key_scopes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False
    )

    api_key: Mapped[ApiKey] = relationship(back_populates="scopes")

    __table_args__ = (UniqueConstraint("api_key_id", "mailbox_id", name="uq_scope_key_mailbox"),)


class Message(Base):
    """An accepted message. Also the queue row.

    Postgres is the queue: workers claim with SELECT ... FOR UPDATE SKIP LOCKED,
    and a reaper returns rows stuck in SENDING past a threshold. That is what
    satisfies "a worker crash must not lose a message" -- no ack protocol, just
    a timestamp.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)  # email_01J...

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="RESTRICT"), nullable=False
    )
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"))

    status: Mapped[MessageStatus] = mapped_column(
        _pg_enum(MessageStatus, "message_status"),
        default=MessageStatus.QUEUED,
        server_default=text("'queued'"),
        nullable=False,
    )

    # Envelope. from_address duplicates mailbox.address at write time so history
    # stays truthful even if a mailbox is later renamed or removed.
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(200))
    to_addresses: Mapped[list] = mapped_column(JSONB, nullable=False)
    cc_addresses: Mapped[list | None] = mapped_column(JSONB)
    bcc_addresses: Mapped[list | None] = mapped_column(JSONB)
    reply_to: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)

    # Return-path for VERP bounce attribution. Pinned in v1 even though bounce
    # PROCESSING is deferred: changing the scheme later invalidates the envelope
    # of every message already sent.
    return_path: Mapped[str | None] = mapped_column(String(320))

    # Purged after terminal state + retention window.
    body_html: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    size_bytes: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    recipient_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # Send accounting. Set when an SMTP attempt BEGINS, regardless of outcome,
    # because the rate limiter must be conservative: counting only successes
    # would let a burst of retries sail past the provider's ceiling, and
    # over-limit there is an unrecoverable 5xx.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Queue mechanics
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Outcome
    provider_response: Mapped[str | None] = mapped_column(Text)
    # How long the provider took to answer, in milliseconds. "Are provider
    # response times changing?" is one of vision.md's questions, and it is also
    # the earliest warning that something upstream is degrading -- latency
    # climbs well before failures start.
    provider_latency_ms: Mapped[int | None] = mapped_column(Integer)
    failure_class: Mapped[FailureClass | None] = mapped_column(
        _pg_enum(FailureClass, "failure_class")
    )
    failure_code: Mapped[int | None] = mapped_column(Integer)
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list[Event]] = relationship(back_populates="message")

    __table_args__ = (
        # The queue claim index. Partial, because only pending work is ever
        # scanned and the terminal rows vastly outnumber it over time.
        Index(
            "ix_messages_queue_claim",
            "next_attempt_at",
            postgresql_where="status IN ('queued', 'temporarily_failed')",
        ),
        # The reaper's index: rows stuck in SENDING after a worker died.
        Index(
            "ix_messages_stale_claims",
            "claimed_at",
            postgresql_where="status = 'sending'",
        ),
        # Body purge sweep.
        Index(
            "ix_messages_body_purge",
            "completed_at",
            postgresql_where="body_purged_at IS NULL AND completed_at IS NOT NULL",
        ),
        Index("ix_messages_project_created", "project_id", "created_at"),
        # The rate limiter's hot query: how many attempts for this sender
        # identity inside the rolling hour?
        Index("ix_messages_rate_window", "mailbox_id", "last_attempt_at"),
        Index("ix_messages_needs_review", "needs_review", postgresql_where="needs_review"),
    )


class Event(Base):
    """Append-only transition log. Never updated, never deleted with the message.

    Events outlive message bodies deliberately: the timeline is what answers
    "where did it fail?", and it must stay answerable after content is purged.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Sanitised. Never bodies, never credentials (release_rules §17, §22).
    detail: Mapped[dict | None] = mapped_column(JSONB)

    occurred_at: Mapped[datetime] = _now_col()

    message: Mapped[Message] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("message_id", "sequence", name="uq_event_message_sequence"),
        Index("ix_events_message", "message_id", "sequence"),
        Index("ix_events_type_time", "event_type", "occurred_at"),
    )


class Suppression(Base):
    """Addresses we refuse to send to.

    Hand-curated at first. Because bad external recipients return 250 Accepted
    and bounce out of band, this list is the only brake that exists until bounce
    collection is built -- and it cannot be reconstructed if lost.
    """

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)  # lowercased
    source: Mapped[SuppressionSource] = mapped_column(
        _pg_enum(SuppressionSource, "suppression_source"),
        default=SuppressionSource.MANUAL,
        server_default=text("'manual'"),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text)

    # Which project's sending produced this entry. Records origin only --
    # ENFORCEMENT stays account-wide, because an address that hard-bounced
    # should not be mailed by anyone here: reputation is shared across every
    # domain on the account, so letting one product keep mailing a dead address
    # damages delivery for the others.
    #
    # It exists so the API listing can be scoped. Without it, GET
    # /v1/suppressions returned every suppressed address on the installation,
    # which let one product enumerate another product's bounced customers.
    #
    # NULL means "before this column existed, or added by the operator", and
    # those stay visible only to the operator.
    project_id: Mapped[int | None] = mapped_column(
        # Named explicitly: an unnamed constraint gets a server-generated name,
        # which a downgrade cannot then reference.
        ForeignKey("projects.id", ondelete="SET NULL", name="fk_suppressions_project"),
        nullable=True,
    )

    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (
        Index("ix_suppressions_address", "address"),
        Index("ix_suppressions_project", "project_id"),
    )


class IdempotencyKey(Base):
    """Replay protection, scoped per project.

    Stores the response so a replay returns byte-identical output. `request_hash`
    catches the dangerous case: the same key reused with a DIFFERENT payload,
    which is a client bug and must be rejected rather than silently served the
    old response.
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = _now_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_idempotency_project_key"),
        Index("ix_idempotency_expires", "expires_at"),
    )


class WorkerHeartbeat(Base):
    """Liveness for a process with no listener.

    The worker deliberately exposes no port -- it only makes outbound SMTP
    connections -- so nothing can probe it over HTTP. It reports into the
    database instead, which is the one thing both processes already share.

    Note this is a supporting signal, not the primary one. Queue age is the
    honest health metric: it catches a dead worker, a stuck rate gate, and a
    provider outage alike, whereas a heartbeat only ever proves the loop is
    turning.
    """

    __tablename__ = "worker_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    version: Mapped[str | None] = mapped_column(String(40))
    last_seen_at: Mapped[datetime] = _now_col()
    messages_processed: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    __table_args__ = (Index("ix_heartbeat_last_seen", "last_seen_at"),)


class SendCounter(Base):
    """Per-mailbox hourly send accounting, shared across workers.

    Lives in Postgres rather than process memory for two reasons: multiple
    workers must share one budget, and the count must survive a restart. Getting
    this wrong means permanently rejected mail, not a retry.

    Window semantics assume ROLLING (the stricter of the two possibilities) until
    measured -- spike_results.md open question #2.
    """

    __tablename__ = "send_counters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # Last observed value of `sent` from the MXRoute API, for drift detection
    # between our accounting and theirs.
    provider_sent_today: Mapped[int | None] = mapped_column(Integer)
    provider_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    drift_ratio: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("mailbox_id", "window_start", name="uq_counter_mailbox_window"),
        Index("ix_send_counters_lookup", "mailbox_id", "window_start"),
    )


class JobType(str, enum.Enum):
    """The CLOSED set of privileged actions the dashboard may request.

    This enum is the security boundary. The API container cannot talk to
    MXRoute -- it holds no credential and mounts no volume containing one -- so
    it asks for work instead. What keeps that from being equivalent to handing
    over the credential is that it can only ask for these, and nothing here
    destroys anything.

    Deliberately absent, and to stay absent: deleting a domain, deleting a
    mailbox, provisioning a mailbox, rotating a password, or anything touching
    reseller users. Provisioning in particular can breach MXRoute's
    acceptable-use policy, which is a judgement call that belongs to a person
    at a terminal.
    """

    ADD_DOMAIN = "add_domain"
    VERIFY_DOMAIN = "verify_domain"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProvisioningJob(Base):
    """A privileged action requested by an unprivileged process.

    The dashboard writes rows here; the provisioner (role=admin, no listener,
    no published port) executes them. Compromising the internet-facing API
    therefore yields the ability to add a domain, not the ability to delete
    every mailbox on the account.

    Claimed with the same FOR UPDATE SKIP LOCKED plus stale-claim reaper the
    message queue uses, so a provisioner killed mid-job does not strand work.
    """

    __tablename__ = "provisioning_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[JobType] = mapped_column(
        _pg_enum(JobType, "job_type"),
        nullable=False,
    )
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"),
        default=JobStatus.PENDING,
        server_default=text("'pending'"),
        nullable=False,
    )

    # Job-type-specific arguments, validated at execution rather than trusted.
    payload: Mapped[dict | None] = mapped_column(JSONB)

    # Who asked. "dashboard" or "cli" -- kept so the audit trail survives even
    # when the job itself has been pruned from the UI.
    requested_by: Mapped[str] = mapped_column(
        String(40), default="dashboard", server_default=text("'dashboard'"), nullable=False
    )

    # Written on both success and failure, and shown to whoever asked. This is
    # the only feedback channel a queued action has.
    result: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _now_col()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_claimable", "status", "created_at"),)
