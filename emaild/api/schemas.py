"""Request and response shapes for /v1.

Deliberately wire-compatible with Resend, because the stated use case is handing
this API to an AI assistant mid-scaffold and having it get the integration right
without being shown docs.

The one intentional divergence is `status`. Resend reports optimistically; we
report what we can actually prove, which is the reason this project exists.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# MXRoute advertises RCPTMAX=150 and SIZE=52428800 in EHLO. Enforced here so a
# request that could never succeed is refused at the edge rather than queued.
MAX_RECIPIENTS = 150
MAX_MESSAGE_BYTES = 52_428_800

Recipients = Annotated[list[str] | str, Field(description="Address, or list of addresses")]


class SendEmailRequest(BaseModel):
    """POST /v1/emails.

    `to`/`cc`/`bcc` accept either a bare string or a list, matching Resend --
    a single-recipient send is by far the common case for transactional mail and
    forcing a one-element list is friction for no benefit.
    """

    model_config = ConfigDict(
        extra="forbid",  # a typo'd field name must not be silently ignored
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    from_: str = Field(
        alias="from",
        min_length=3,
        description="Sender. 'noreply@example.com' or 'Acme <noreply@example.com>'.",
    )
    to: Recipients
    subject: str | None = Field(default=None, max_length=998)  # RFC 5322 line limit
    html: str | None = None
    text: str | None = None
    cc: Recipients | None = None
    bcc: Recipients | None = None
    reply_to: str | None = Field(default=None, alias="reply_to")
    headers: dict[str, str] | None = None
    tags: dict[str, str] | None = None

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _coerce_to_list(cls, v: Any) -> Any:
        if v is None or isinstance(v, list):
            return v
        if isinstance(v, str):
            return [v]
        raise ValueError("must be a string or a list of strings")

    @model_validator(mode="after")
    def _require_a_body(self) -> SendEmailRequest:
        if not (self.html or self.text):
            raise ValueError("provide at least one of 'html' or 'text'")
        return self

    @model_validator(mode="after")
    def _require_a_recipient(self) -> SendEmailRequest:
        if not self.to:
            raise ValueError("'to' must contain at least one recipient")
        return self

    def all_recipients(self) -> list[str]:
        out: list[str] = []
        for group in (self.to, self.cc, self.bcc):
            if isinstance(group, list):
                out.extend(group)
        return out

    def estimated_size_bytes(self) -> int:
        """Approximate the on-wire size for limit checking.

        Deliberately an estimate: the exact MIME body is assembled at send time.
        A generous header allowance keeps this an over-estimate, so a message
        that passes here does not fail at the provider -- erring the other way
        would mean accepting work that can never succeed.
        """
        size = sum(len((v or "").encode("utf-8")) for v in (self.subject, self.html, self.text))
        size += sum(len(r.encode("utf-8")) + 8 for r in self.all_recipients())
        size += len(self.from_.encode("utf-8"))
        if self.headers:
            size += sum(len(f"{k}: {v}\r\n".encode()) for k, v in self.headers.items())
        return size + 2048  # envelope, MIME boundaries, Received headers


class SendEmailResponse(BaseModel):
    id: str
    status: str


class EventView(BaseModel):
    type: str
    occurred_at: str
    detail: dict[str, Any] | None = None


class MessageView(BaseModel):
    """GET /v1/emails/{id}.

    Never includes the body: it may already have been purged, and re-serving
    verification links or password-reset tokens from an API is precisely the
    accidental archive vision.md refuses to build.
    """

    id: str
    status: str
    from_address: str = Field(serialization_alias="from")
    to: list[str]
    cc: list[str] | None = None
    bcc: list[str] | None = None
    subject: str | None = None
    created_at: str
    completed_at: str | None = None
    attempts: int
    failure_class: str | None = None
    failure_code: int | None = None
    provider_response: str | None = None
    events: list[EventView]
