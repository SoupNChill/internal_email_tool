"""Configuration, with role-scoped secret loading.

The central idea (deployment_and_release.md §3): the three processes need
different secrets, so they should not be able to read each other's. The public
API never holds the MXRoute admin credential, which means compromising the
internet-facing surface yields sending within existing scopes -- not the ability
to delete mailboxes or manipulate reseller users.

This is enforced at startup rather than by convention, because a convention that
is only documented is a convention that eventually gets violated.
"""

from __future__ import annotations

import enum
import os
from functools import lru_cache

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(str, enum.Enum):
    """Which process this is. Determines which secrets may be loaded."""

    API = "api"
    WORKER = "worker"
    ADMIN = "admin"


class Environment(str, enum.Enum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


def _env_file_for_process() -> str | None:
    """Which .env file this process should read, if any.

    `.env` holds every variable for every role, because Docker Compose reads it
    to build each service's environment. But Settings reads that same file
    directly when run on the host -- which means a host-side `role=worker` would
    pick up the MXRoute admin key out of the shared file and be refused by the
    scoping guard, even though the real worker container never receives it.

    So the file is selectable:
      EMAILD_ENV_FILE=none          -- read the process environment only
      EMAILD_ENV_FILE=.env.worker   -- read a role-scoped file
      (unset)                       -- read .env, the compose/admin default

    NOTE: this is evaluated when the Settings class is DEFINED, so the variable
    must be set before `emaild.config` is first imported. Setting it later has
    no effect. See tests/conftest.py, which relies on exactly this.
    """
    override = os.environ.get("EMAILD_ENV_FILE")
    if override is None:
        return ".env"
    if override.strip().lower() in ("", "none", "0", "false"):
        return None
    return override


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMAILD_",
        env_file=_env_file_for_process(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    role: Role = Role.API
    env: Environment = Environment.DEVELOPMENT
    database_url: str
    log_level: str = "INFO"

    # --- Secrets. Presence is validated against `role` below. ---
    mailbox_encryption_key: str | None = None
    mxroute_server: str | None = None
    mxroute_username: str | None = None
    mxroute_api_key: str | None = None

    # --- Provider limits (spike_results.md) ---
    smtp_port: int = 465
    smtp_timeout_seconds: int = 30
    fallback_max_message_bytes: int = 52_428_800
    fallback_max_recipients: int = 150
    fallback_max_messages_per_connection: int = 100

    hourly_limit: int = 400
    rate_limit_safety_margin: float = Field(default=0.9, gt=0.0, le=0.95)

    # --- Retention ---
    body_retention_hours: int = 72
    idempotency_ttl_hours: int = 24

    # --- Network ---
    trusted_proxy_hosts: str = ""

    # --- Dashboard ---
    dashboard_enabled: bool = True
    dashboard_token: str | None = None
    # Explicit acknowledgement that something in front (Cloudflare Access) is
    # authenticating the dashboard. Required to serve it unauthenticated in
    # production -- see the validator below.
    dashboard_behind_proxy_auth: bool = False

    @property
    def effective_hourly_limit(self) -> int:
        """The ceiling we actually enforce, below MXRoute's.

        Over-limit is a permanent 5xx with no provider-side queue, so our
        backpressure has to engage before theirs does. Floor of 1 keeps this
        sane if someone configures an absurdly small limit.
        """
        return max(1, int(self.hourly_limit * self.rate_limit_safety_margin))

    @property
    def trusted_proxies(self) -> list[str]:
        return [h.strip() for h in self.trusted_proxy_hosts.split(",") if h.strip()]

    @field_validator("rate_limit_safety_margin")
    @classmethod
    def _margin_leaves_headroom(cls, v: float) -> float:
        # A margin of 1.0 means aiming exactly at a ceiling whose window
        # semantics we have not confirmed (rolling vs fixed -- spike_results.md
        # open question #2). Refuse it rather than discover the difference in
        # production as permanently rejected mail.
        if v > 0.95:
            raise ValueError(
                "rate_limit_safety_margin must leave headroom (<= 0.95); "
                "over-limit sends are permanently rejected, not deferred"
            )
        return v

    @field_validator("mailbox_encryption_key")
    @classmethod
    def _key_looks_like_fernet(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None or v == "":
            return None
        if len(v) != 44:
            raise ValueError(
                f"{info.field_name} must be a 44-character urlsafe-base64 Fernet key; "
                'generate with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        return v

    @model_validator(mode="after")
    def _enforce_role_secret_scoping(self) -> Settings:
        """Fail loudly when a role holds a secret it should not, or lacks one it needs.

        release_rules §23: no silent fallback to unsafe behaviour. A misconfigured
        process must refuse to start rather than run with the wrong authority.
        """
        has_mxroute = any((self.mxroute_server, self.mxroute_username, self.mxroute_api_key))

        if self.role in (Role.API, Role.WORKER) and has_mxroute:
            raise ValueError(
                f"role={self.role.value} must not be given MXROUTE_* credentials. "
                "The account-root key belongs only to role=admin, which is not "
                "publicly routed. See deployment_and_release.md §3."
            )

        if self.role is Role.ADMIN:
            missing = [
                name
                for name, val in (
                    ("EMAILD_MXROUTE_SERVER", self.mxroute_server),
                    ("EMAILD_MXROUTE_USERNAME", self.mxroute_username),
                    ("EMAILD_MXROUTE_API_KEY", self.mxroute_api_key),
                )
                if not val
            ]
            if missing:
                raise ValueError(f"role=admin requires: {', '.join(missing)}")

        if self.role in (Role.WORKER, Role.ADMIN) and not self.mailbox_encryption_key:
            raise ValueError(
                f"role={self.role.value} requires EMAILD_MAILBOX_ENCRYPTION_KEY "
                "to decrypt mailbox SMTP passwords"
            )

        if self.role is Role.API and self.mailbox_encryption_key:
            raise ValueError(
                "role=api must not hold EMAILD_MAILBOX_ENCRYPTION_KEY. The API "
                "never sends mail directly; only the worker decrypts credentials."
            )

        if self.env is Environment.PRODUCTION and "CHANGEME" in self.database_url:
            raise ValueError("refusing to start in production with a placeholder database password")

        # Fail closed. The dashboard shows recipient addresses, subjects, and
        # volume -- real data. In development it binds to localhost and needs no
        # gate, but in production an unauthenticated dashboard must be a
        # deliberate, stated decision rather than an oversight.
        if (
            self.role is Role.API
            and self.env is Environment.PRODUCTION
            and self.dashboard_enabled
            and not self.dashboard_token
            and not self.dashboard_behind_proxy_auth
        ):
            raise ValueError(
                "refusing to serve an unauthenticated dashboard in production. "
                "Either set EMAILD_DASHBOARD_TOKEN, or set "
                "EMAILD_DASHBOARD_BEHIND_PROXY_AUTH=true to confirm Cloudflare "
                "Access (or equivalent) is authenticating it. To turn the "
                "dashboard off entirely, set EMAILD_DASHBOARD_ENABLED=false."
            )

        return self

    def redacted(self) -> dict[str, object]:
        """Config shape for diagnostics. release_rules §17, §48: never the values."""
        return {
            "role": self.role.value,
            "env": self.env.value,
            "database": _redact_dsn(self.database_url),
            "smtp_port": self.smtp_port,
            "hourly_limit": self.hourly_limit,
            "effective_hourly_limit": self.effective_hourly_limit,
            "body_retention_hours": self.body_retention_hours,
            "mailbox_encryption_key": "set" if self.mailbox_encryption_key else "unset",
            "mxroute_credentials": "set" if self.mxroute_api_key else "unset",
            "trusted_proxies": len(self.trusted_proxies),
        }


def _redact_dsn(dsn: str) -> str:
    """postgresql+asyncpg://user:pw@host/db -> postgresql+asyncpg://user:***@host/db"""
    if "@" not in dsn or "//" not in dsn:
        return "***"
    scheme, _, rest = dsn.partition("//")
    creds, _, location = rest.rpartition("@")
    if ":" in creds:
        user, _, _ = creds.partition(":")
        return f"{scheme}//{user}:***@{location}"
    return f"{scheme}//***@{location}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from env
