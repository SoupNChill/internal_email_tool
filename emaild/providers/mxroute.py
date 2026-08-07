"""MXRoute control-plane client (DirectAdmin REST wrapper).

This is the *control* plane only -- domains, mailboxes, DNS requirements, quota.
It cannot send mail. Delivery is SMTP and lives in the Phase 5 adapter.

Two things shape this client:

* **Rate limits are tight**: 100 reads/minute and 20 writes/minute, account-wide
  and shared across everything we do. We self-throttle rather than discover the
  limit as 429s, because a burst of provisioning must not starve the domain
  verification sweep.
* **The published OpenAPI spec is not a reliable contract.** `GET
  /domains/{d}/mail-status` is documented but answers 405 on a live account. The
  client degrades on unexpected shapes instead of assuming the spec is truth.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.mxroute.com"

# Documented limits. We pace below them; see RateLimiter.
READ_LIMIT_PER_MINUTE = 100
WRITE_LIMIT_PER_MINUTE = 20
_SAFETY = 0.8  # use at most 80% of the documented budget

_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class MXRouteError(Exception):
    """Base class for provider failures."""


class MXRouteAuthError(MXRouteError):
    """Credentials rejected. Not retryable -- alert rather than back off."""


class MXRouteNotFound(MXRouteError):
    pass


class MXRouteConflict(MXRouteError):
    """409 -- the resource already exists. Usually benign and idempotent-ish."""


class MXRouteUnavailable(MXRouteError):
    """Endpoint missing or method not allowed: spec/implementation divergence."""


class MXRouteAPIError(MXRouteError):
    def __init__(self, code: str, message: str, field_name: str | None = None) -> None:
        self.code = code
        self.message = message
        self.field_name = field_name
        super().__init__(f"{code}: {message}" + (f" (field={field_name})" if field_name else ""))


@dataclass
class RateLimiter:
    """Sliding-window pacing for read and write budgets, tracked separately.

    Reads and writes have very different ceilings (100 vs 20), so one shared
    counter would either waste read budget or blow through the write budget.
    """

    read_limit: int = int(READ_LIMIT_PER_MINUTE * _SAFETY)
    write_limit: int = int(WRITE_LIMIT_PER_MINUTE * _SAFETY)
    _reads: list[float] = field(default_factory=list)
    _writes: list[float] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, method: str) -> None:
        is_write = method.upper() in _WRITE_METHODS
        async with self._lock:
            while True:
                now = time.monotonic()
                bucket = self._writes if is_write else self._reads
                limit = self.write_limit if is_write else self.read_limit

                bucket[:] = [t for t in bucket if now - t < 60.0]
                if len(bucket) < limit:
                    bucket.append(now)
                    return

                # Sleep until the oldest call ages out of the window.
                wait = 60.0 - (now - bucket[0]) + 0.05
                log.info(
                    "mxroute: pacing %s request for %.1fs (%d/%d used in window)",
                    "write" if is_write else "read",
                    wait,
                    len(bucket),
                    limit,
                )
                await asyncio.sleep(wait)


class MXRouteClient:
    """Async client for the MXRoute control plane.

    Constructed only by role=admin. The credential it holds is account-root: it
    can delete mailboxes and manage reseller users, not merely read.
    """

    def __init__(
        self,
        server: str,
        username: str,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._limiter = limiter or RateLimiter()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "X-Server": server,
                "X-Username": username,
                "X-API-Key": api_key,
                "Accept": "application/json",
            },
        )

    async def __aenter__(self) -> MXRouteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # -- request plumbing ---------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> Any:
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            await self._limiter.acquire(method)
            try:
                response = await self._client.request(method, path, json=json)
            except httpx.TimeoutException:
                last_exc = MXRouteError(f"timeout calling {method} {path}")
                log.warning("mxroute: timeout on %s %s (attempt %d)", method, path, attempt)
            except httpx.HTTPError as exc:
                last_exc = MXRouteError(f"transport error calling {method} {path}: {exc}")
                log.warning("mxroute: transport error on %s %s: %s", method, path, exc)
            else:
                if response.status_code == 429:
                    # Should be rare -- the limiter paces below the documented
                    # ceiling -- but honour Retry-After if the server disagrees.
                    delay = float(response.headers.get("Retry-After", 5))
                    log.warning("mxroute: 429 on %s %s, sleeping %.1fs", method, path, delay)
                    await asyncio.sleep(delay)
                    last_exc = MXRouteError("rate limited by provider")
                    continue

                self._log_budget(response)
                return self._handle(response, method, path)

            if attempt < max_attempts:
                await asyncio.sleep(min(2**attempt, 8))

        # Reached only when every attempt failed. The fallback exists because an
        # `assert` here would vanish under `python -O`, leaving the function to
        # return None and the caller to fail somewhere far less informative.
        raise last_exc or MXRouteError(f"{method} {path} failed after {max_attempts} attempts")

    def _log_budget(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) < 10:
            log.warning("mxroute: rate-limit budget low (%s remaining)", remaining)

    def _handle(self, response: httpx.Response, method: str, path: str) -> Any:
        status = response.status_code

        if status in (401, 403):
            raise MXRouteAuthError(f"credentials rejected on {method} {path} ({status})")
        if status in (404, 405) and response.request.method != "GET":
            raise MXRouteUnavailable(f"{method} {path} unavailable ({status})")
        if status == 405:
            # Documented in the spec but absent in the implementation. Callers
            # degrade rather than treating this as a hard failure.
            raise MXRouteUnavailable(f"{method} {path} not implemented by provider (405)")
        if status == 409:
            raise MXRouteConflict(f"{method} {path} conflicts with existing resource")

        if status == 204 or not response.content:
            return None

        try:
            payload = response.json()
        except ValueError:
            raise MXRouteError(
                f"{method} {path} returned non-JSON ({status}); " f"{len(response.content)} bytes"
            ) from None

        if isinstance(payload, dict) and payload.get("success") is False:
            err = payload.get("error") or {}
            code = str(err.get("code", "UNKNOWN"))
            if code == "NOT_FOUND" or status == 404:
                raise MXRouteNotFound(str(err.get("message", "not found")))
            raise MXRouteAPIError(code, str(err.get("message", "")), err.get("field"))

        if status >= 400:
            raise MXRouteError(f"{method} {path} failed with HTTP {status}")

        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # -- account ------------------------------------------------------------

    async def get_verification_key(self) -> dict[str, Any]:
        """The TXT record that must exist BEFORE a domain can be added."""
        return dict(await self._request("GET", "/verification-key") or {})

    async def get_quota(self) -> dict[str, Any]:
        return dict(await self._request("GET", "/quota") or {})

    # -- domains ------------------------------------------------------------

    async def list_domains(self) -> list[str]:
        data = await self._request("GET", "/domains")
        return list(data or [])

    async def create_domain(self, domain: str) -> dict[str, Any]:
        return dict(await self._request("POST", "/domains", json={"domain": domain}) or {})

    async def get_dns(self, domain: str) -> dict[str, Any]:
        """The authoritative source for MX host, SPF, DKIM, and the ownership token.

        Note there is no DMARC field: MXRoute does not supply one, so verification
        must not wait for a record the provider will never return.
        """
        return dict(await self._request("GET", f"/domains/{domain}/dns") or {})

    # -- mailboxes ----------------------------------------------------------

    async def list_email_accounts(self, domain: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/domains/{domain}/email-accounts")
        return list(data or [])

    async def create_email_account(
        self,
        domain: str,
        username: str,
        password: str,
        *,
        quota_mb: int = 1024,
        daily_limit: int = 9600,
    ) -> Any:
        return await self._request(
            "POST",
            f"/domains/{domain}/email-accounts",
            json={
                "username": username,
                "password": password,
                "quota": quota_mb,
                "limit": daily_limit,
            },
        )

    async def get_email_account(self, domain: str, username: str) -> dict[str, Any]:
        """Includes live `sent` and `limit` counters -- our drift check against
        the provider's own accounting."""
        data = await self._request("GET", f"/domains/{domain}/email-accounts/{username}")
        return dict(data or {})

    async def update_email_account(
        self,
        domain: str,
        username: str,
        *,
        password: str | None = None,
        quota_mb: int | None = None,
        daily_limit: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {}
        if password is not None:
            body["password"] = password
        if quota_mb is not None:
            body["quota"] = quota_mb
        if daily_limit is not None:
            body["limit"] = daily_limit
        if not body:
            raise ValueError("update_email_account requires at least one field to change")
        return await self._request(
            "PATCH", f"/domains/{domain}/email-accounts/{username}", json=body
        )
