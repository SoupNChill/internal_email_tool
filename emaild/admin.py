"""Admin CLI -- the role=admin entry point.

This is the only surface that holds the MXRoute account-root credential, and it
is deliberately never routed through the tunnel. Run it on the host:

    python -m emaild.admin domains list
    python -m emaild.admin domains records example.com
    python -m emaild.admin domains verify example.com
    python -m emaild.admin mailboxes provision noreply@example.com

Eventually folded into `appctl` (first_production_packaging §16). Built on
argparse rather than a CLI framework -- release_rules §18 discourages adding a
dependency for something the standard library already does well.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from sqlalchemy import select

from emaild.config import Role, Settings, get_settings
from emaild.crypto import MailboxCipher, generate_api_key
from emaild.db import dispose_engine, init_engine, session_scope
from emaild.domains import (
    add_domain,
    get_verification_record,
    refresh_domain,
    required_dns_records,
)
from emaild.logging_config import configure_logging
from emaild.models import ApiKey, ApiKeyScope, Domain, Mailbox, Project
from emaild.providers.mxroute import MXRouteClient, MXRouteError
from emaild.provisioning import (
    PolicyViolation,
    ProvisioningError,
    provider_usage,
    provision_mailbox,
    rotate_mailbox_password,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 1
EXIT_POLICY = 3


@contextlib.asynccontextmanager
async def _provider(settings: Settings) -> AsyncIterator[MXRouteClient]:
    # Not `assert`: asserts are stripped under `python -O`, which would turn a
    # missing-credential guard into a confusing TypeError at request time.
    if not (settings.mxroute_server and settings.mxroute_username and settings.mxroute_api_key):
        raise RuntimeError("MXRoute credentials missing; role=admin requires all three")
    async with MXRouteClient(
        settings.mxroute_server, settings.mxroute_username, settings.mxroute_api_key
    ) as client:
        yield client


def _cipher(settings: Settings) -> MailboxCipher:
    if not settings.mailbox_encryption_key:
        raise RuntimeError("EMAILD_MAILBOX_ENCRYPTION_KEY is required to handle credentials")
    return MailboxCipher(settings.mailbox_encryption_key)


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(str(r[i]).ljust(widths[i]) for i in range(len(headers))) for r in rows]
    return "\n".join([line, sep, *body])


# --- domains ---------------------------------------------------------------


async def cmd_domains_list(args: argparse.Namespace, settings: Settings) -> int:
    async with session_scope() as session:
        domains = (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()
    if not domains:
        print("No domains tracked. Add one with: domains add <name>")
        return EXIT_OK
    rows = [
        [
            d.name,
            d.status.value,
            d.smtp_host or "-",
            d.dns_checked_at.strftime("%Y-%m-%d %H:%M") if d.dns_checked_at else "never",
        ]
        for d in domains
    ]
    print(_table(rows, ["DOMAIN", "STATUS", "SMTP HOST", "LAST CHECKED"]))
    return EXIT_OK


async def cmd_domains_token(args: argparse.Namespace, settings: Settings) -> int:
    async with _provider(settings) as client:
        record = await get_verification_record(client)
    print("Publish this TXT record BEFORE adding any new domain:\n")
    print(f"  Type:  {record['type']}")
    print(f"  Name:  {record['name']}")
    print(f"  Value: {record['value']}\n")
    print("Then wait for propagation (MXRoute suggests 5-15 minutes) before 'domains add'.")
    return EXIT_OK


async def cmd_domains_add(args: argparse.Namespace, settings: Settings) -> int:
    async with _provider(settings) as client, session_scope() as session:
        try:
            domain = await add_domain(session, client, args.domain)
        except MXRouteError as exc:
            print(f"Failed to add {args.domain}: {exc}", file=sys.stderr)
            print(
                "\nMost often this means the ownership TXT record is not yet "
                "resolving. Run 'domains token' and publish it first.",
                file=sys.stderr,
            )
            return EXIT_FAILED
        print(f"Tracking {domain.name} (status: {domain.status.value})")
    print(f"\nNext: python -m emaild.admin domains records {args.domain}")
    return EXIT_OK


async def cmd_domains_records(args: argparse.Namespace, settings: Settings) -> int:
    async with _provider(settings) as client:
        dns_info = await client.get_dns(args.domain)
    records = required_dns_records(args.domain, dns_info)

    print(f"DNS records required for {args.domain}:\n")
    for r in records:
        prio = f"  (priority {r['priority']})" if r["priority"] else ""
        print(f"  {r['type']:<4} {r['name']}{prio}")
        print(f"       {r['value']}\n")
    if not (dns_info.get("dkim") or {}).get("value"):
        print("WARNING: MXRoute has not generated a DKIM key for this domain.")
        print("         The domain cannot reach 'ready' until it exists.\n")
    print("Paste values WITHOUT surrounding quotes -- most registrars add their own.")
    return EXIT_OK


async def cmd_domains_verify(args: argparse.Namespace, settings: Settings) -> int:
    async with _provider(settings) as client, session_scope() as session:
        targets = (
            [args.domain]
            if args.domain
            else [d.name for d in (await session.execute(select(Domain))).scalars().all()]
        )
        if not targets:
            print("No domains tracked.")
            return EXIT_OK

        worst = EXIT_OK
        for name in targets:
            result = await refresh_domain(session, client, name)
            arrow = (
                f"{result.previous.value} -> {result.current.value}"
                if result.changed
                else result.current.value
            )
            print(f"\n{name}: {arrow}")
            if result.note:
                print(f"  {result.note}")
            if result.report is None:
                worst = EXIT_FAILED
                continue
            for key, check in result.report.checks.items():
                mark = {"pass": "ok  ", "fail": "FAIL", "missing": "MISS", "error": "ERR "}[
                    check.result.value
                ]
                detail = f"  ({check.detail})" if check.detail else ""
                print(f"    [{mark}] {key}{detail}")
            if not result.report.can_send:
                worst = EXIT_FAILED
        return worst


# --- mailboxes -------------------------------------------------------------


async def cmd_mailboxes_list(args: argparse.Namespace, settings: Settings) -> int:
    async with session_scope() as session:
        rows_db = (
            await session.execute(select(Mailbox, Domain).join(Domain).order_by(Mailbox.address))
        ).all()
    if not rows_db:
        print("No mailboxes provisioned.")
        return EXIT_OK
    rows = [
        [m.address, d.status.value, str(m.hourly_limit), "yes" if m.active else "no"]
        for m, d in rows_db
    ]
    print(_table(rows, ["ADDRESS", "DOMAIN STATUS", "HOURLY", "ACTIVE"]))
    return EXIT_OK


async def cmd_mailboxes_provision(args: argparse.Namespace, settings: Settings) -> int:
    async with _provider(settings) as client, session_scope() as session:
        try:
            mailbox, password = await provision_mailbox(
                session,
                client,
                _cipher(settings),
                address=args.address,
                display_name=args.display_name,
                allow_additional_identity=args.additional_identity,
            )
        except PolicyViolation as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return EXIT_POLICY
        except ProvisioningError as exc:
            print(f"Failed: {exc}", file=sys.stderr)
            return EXIT_FAILED

        print(f"Provisioned {mailbox.address}")
        print(f"  SMTP password: {password}")
        print("\n  Shown once. It is stored encrypted and never logged.")
        print("  The envelope sender is pinned to this exact address by MXRoute.")
    return EXIT_OK


async def cmd_mailboxes_usage(args: argparse.Namespace, settings: Settings) -> int:
    async with _provider(settings) as client:
        usage = await provider_usage(client, args.address)
    print(f"{args.address}")
    print(f"  sent today : {usage['sent_today']} / {usage['daily_limit']}")
    print(f"  suspended  : {usage['suspended']}")
    print("\n  Note: this is the provider's DAILY counter. The binding constraint")
    print("  is 400/hour, which MXRoute does not expose -- we track it locally.")
    return EXIT_OK


async def cmd_mailboxes_rotate(args: argparse.Namespace, settings: Settings) -> int:
    async with _provider(settings) as client, session_scope() as session:
        try:
            password = await rotate_mailbox_password(
                session, client, _cipher(settings), args.address
            )
        except ProvisioningError as exc:
            print(f"Failed: {exc}", file=sys.stderr)
            return EXIT_FAILED
    print(f"Rotated password for {args.address}")
    print(f"  New SMTP password: {password}")
    return EXIT_OK


# --- projects and keys -----------------------------------------------------


async def cmd_projects_create(args: argparse.Namespace, settings: Settings) -> int:
    async with session_scope() as session:
        existing = (
            await session.execute(select(Project).where(Project.name == args.name))
        ).scalar_one_or_none()
        if existing:
            print(f"Project already exists: {args.name}", file=sys.stderr)
            return EXIT_FAILED
        session.add(Project(name=args.name, description=args.description))
    print(f"Created project {args.name}")
    return EXIT_OK


async def cmd_projects_list(args: argparse.Namespace, settings: Settings) -> int:
    async with session_scope() as session:
        projects = (await session.execute(select(Project).order_by(Project.name))).scalars().all()
    if not projects:
        print("No projects.")
        return EXIT_OK
    print(
        _table(
            [[p.name, p.description or "-", "yes" if p.active else "no"] for p in projects],
            ["PROJECT", "DESCRIPTION", "ACTIVE"],
        )
    )
    return EXIT_OK


async def cmd_keys_create(args: argparse.Namespace, settings: Settings) -> int:
    async with session_scope() as session:
        project = (
            await session.execute(select(Project).where(Project.name == args.project))
        ).scalar_one_or_none()
        if project is None:
            print(f"No such project: {args.project}", file=sys.stderr)
            return EXIT_FAILED

        mailboxes = (
            (await session.execute(select(Mailbox).where(Mailbox.address.in_(args.mailbox))))
            .scalars()
            .all()
        )
        found = {m.address for m in mailboxes}
        missing = set(args.mailbox) - found
        if missing:
            print(f"Unknown mailboxes: {', '.join(sorted(missing))}", file=sys.stderr)
            return EXIT_FAILED

        full_key, key_hash, prefix = generate_api_key()
        api_key = ApiKey(
            project_id=project.id, name=args.name, key_hash=key_hash, key_prefix=prefix
        )
        session.add(api_key)
        await session.flush()
        for mailbox in mailboxes:
            session.add(ApiKeyScope(api_key_id=api_key.id, mailbox_id=mailbox.id))

    print(f"Created key '{args.name}' for project {args.project}")
    print(f"\n  {full_key}\n")
    print("  Shown once and never recoverable -- only a SHA-256 hash is stored.")
    print(f"  Scoped to: {', '.join(sorted(found))}")
    return EXIT_OK


async def cmd_keys_list(args: argparse.Namespace, settings: Settings) -> int:
    async with session_scope() as session:
        keys = (
            await session.execute(select(ApiKey, Project).join(Project).order_by(ApiKey.name))
        ).all()
    if not keys:
        print("No API keys.")
        return EXIT_OK
    rows = [
        [
            k.name,
            p.name,
            f"{k.key_prefix}...",
            "revoked" if k.revoked_at else ("active" if k.active else "inactive"),
            k.last_used_at.strftime("%Y-%m-%d %H:%M") if k.last_used_at else "never",
        ]
        for k, p in keys
    ]
    print(_table(rows, ["NAME", "PROJECT", "PREFIX", "STATE", "LAST USED"]))
    return EXIT_OK


# --- wiring ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m emaild.admin",
        description="emaild administration (role=admin; never publicly routed)",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    d = sub.add_parser("domains", help="domain lifecycle").add_subparsers(dest="cmd", required=True)
    d.add_parser("list", help="show tracked domains").set_defaults(fn=cmd_domains_list)
    d.add_parser("token", help="show the account ownership TXT record").set_defaults(
        fn=cmd_domains_token
    )
    p = d.add_parser("add", help="track a domain and register it with MXRoute")
    p.add_argument("domain")
    p.set_defaults(fn=cmd_domains_add)
    p = d.add_parser("records", help="show the DNS records to publish")
    p.add_argument("domain")
    p.set_defaults(fn=cmd_domains_records)
    p = d.add_parser("verify", help="re-check DNS and update status")
    p.add_argument("domain", nargs="?", help="omit to verify every tracked domain")
    p.set_defaults(fn=cmd_domains_verify)

    m = sub.add_parser("mailboxes", help="sender identities").add_subparsers(
        dest="cmd", required=True
    )
    m.add_parser("list", help="show provisioned mailboxes").set_defaults(fn=cmd_mailboxes_list)
    p = m.add_parser("provision", help="create a sender identity")
    p.add_argument("address")
    p.add_argument("--display-name", default=None)
    p.add_argument(
        "--additional-identity",
        action="store_true",
        help="confirm this is a genuinely distinct sender, not extra send budget",
    )
    p.set_defaults(fn=cmd_mailboxes_provision)
    p = m.add_parser("usage", help="provider-reported send counters")
    p.add_argument("address")
    p.set_defaults(fn=cmd_mailboxes_usage)
    p = m.add_parser("rotate", help="rotate the SMTP password")
    p.add_argument("address")
    p.set_defaults(fn=cmd_mailboxes_rotate)

    pr = sub.add_parser("projects", help="sending projects").add_subparsers(
        dest="cmd", required=True
    )
    pr.add_parser("list").set_defaults(fn=cmd_projects_list)
    p = pr.add_parser("create")
    p.add_argument("name")
    p.add_argument("--description", default=None)
    p.set_defaults(fn=cmd_projects_create)

    k = sub.add_parser("keys", help="scoped API keys").add_subparsers(dest="cmd", required=True)
    k.add_parser("list").set_defaults(fn=cmd_keys_list)
    p = k.add_parser("create")
    p.add_argument("name")
    p.add_argument("--project", required=True)
    p.add_argument("--mailbox", required=True, action="append", help="repeatable; scopes the key")
    p.set_defaults(fn=cmd_keys_create)

    return parser


async def _run(fn: Callable[..., Coroutine[Any, Any, int]], args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    if settings.role is not Role.ADMIN:
        print(
            f"This CLI requires EMAILD_ROLE=admin (currently '{settings.role.value}'). "
            "It is the only role permitted to hold the MXRoute account-root key.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    init_engine(settings)
    try:
        return await fn(args, settings)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args.fn, args))
    except KeyboardInterrupt:
        return 130
    except MXRouteError as exc:
        print(f"Provider error: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
