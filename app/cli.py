"""Operational CLI.

    python -m app.cli generate-key
    python -m app.cli create-api-key --name "Claude Code"
    python -m app.cli add-credential --name "Account 1" --secret-env QWEN_TOKEN
    python -m app.cli list-credentials
    python -m app.cli health

Secrets are read from environment variables or an interactive prompt so they
never land in shell history or process listings.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.config import get_settings
from app.database import init_db, session_scope
from app.security.crypto import generate_secret_key
from app.services import api_key_service, credential_service, model_service
from app.utils.logging import configure_logging


async def _cmd_create_api_key(args: argparse.Namespace) -> int:
    await init_db()
    async with session_scope() as session:
        record, plaintext = await api_key_service.create_api_key(
            session,
            name=args.name,
            description=args.description,
            expires_in_days=args.expires_in_days,
        )
    print(f"API key created (id={record.id}, name={record.name!r}).")
    print("Store this value now — it cannot be retrieved again:\n")
    print(f"  {plaintext}\n")
    return 0


async def _cmd_add_credential(args: argparse.Namespace) -> int:
    secret = _read_secret(args.secret_env, "Qwen credential secret: ")
    if not secret:
        print("error: no secret provided", file=sys.stderr)
        return 2
    refresh = _read_secret(args.refresh_env, "", optional=True) if args.refresh_env else None
    await init_db()
    async with session_scope() as session:
        credential = await credential_service.create_credential(
            session,
            name=args.name,
            secret=secret,
            refresh_secret=refresh,
            auth_mode=args.auth_mode,
            base_url=args.base_url,
        )
    print(
        f"Credential stored (id={credential.id}, name={credential.name!r}, "
        f"mode={credential.auth_mode}, secret={credential.secret_hint})."
    )
    return 0


async def _cmd_list_credentials(_: argparse.Namespace) -> int:
    await init_db()
    async with session_scope() as session:
        rows = await credential_service.list_credentials(session)
    if not rows:
        print("No credentials stored.")
        return 0
    print(f"{'ID':<4} {'NAME':<24} {'MODE':<8} {'STATUS':<10} {'SECRET':<20} ENABLED")
    for row in rows:
        print(
            f"{row.id:<4} {row.name[:23]:<24} {row.auth_mode:<8} "
            f"{row.status:<10} {row.secret_hint:<20} {row.enabled}"
        )
    return 0


async def _cmd_test_credential(args: argparse.Namespace) -> int:
    await init_db()
    async with session_scope() as session:
        result = await credential_service.test_credential(session, args.id)
    print(result)
    return 0 if result.get("healthy") else 1


async def _cmd_discover_models(_: argparse.Namespace) -> int:
    await init_db()
    async with session_scope() as session:
        rows = await model_service.discover_models(session)
    for row in rows:
        print(f"{row.model_id:<28} aliases={row.aliases or '-'}")
    return 0


async def _cmd_health(_: argparse.Namespace) -> int:
    settings = get_settings()
    await init_db()
    from app.gateway.scheduler import get_scheduler

    async with session_scope() as session:
        stats = await get_scheduler().stats(session, provider=settings.default_provider)
    print(
        f"provider={settings.default_provider} total={stats.total} "
        f"healthy={stats.healthy} cooldown={stats.cooling_down} "
        f"disabled={stats.disabled} expired={stats.expired}"
    )
    return 0 if stats.healthy > 0 else 1


def _read_secret(env_var: str | None, prompt: str, optional: bool = False) -> str | None:
    if env_var:
        value = os.environ.get(env_var, "").strip()
        if value:
            return value
        if optional:
            return None
        print(f"error: environment variable {env_var} is empty", file=sys.stderr)
        return None
    if optional:
        return None
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-key", help="Print a new GATEWAY_SECRET_KEY value")

    p = sub.add_parser("create-api-key", help="Create a client API key")
    p.add_argument("--name", required=True)
    p.add_argument("--description", default=None)
    p.add_argument("--expires-in-days", type=int, default=None)
    p.set_defaults(func=_cmd_create_api_key)

    p = sub.add_parser("add-credential", help="Store a Qwen credential")
    p.add_argument("--name", required=True)
    p.add_argument(
        "--secret-env",
        default=None,
        help="Environment variable holding the secret (otherwise prompt)",
    )
    p.add_argument("--refresh-env", default=None, help="Env var holding a refresh token")
    p.add_argument("--auth-mode", default="auto", choices=["auto", "portal", "web"])
    p.add_argument("--base-url", default=None)
    p.set_defaults(func=_cmd_add_credential)

    p = sub.add_parser("list-credentials", help="List stored credentials (masked)")
    p.set_defaults(func=_cmd_list_credentials)

    p = sub.add_parser("test-credential", help="Probe a credential upstream")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(func=_cmd_test_credential)

    p = sub.add_parser("discover-models", help="Refresh the model catalogue")
    p.set_defaults(func=_cmd_discover_models)

    p = sub.add_parser("health", help="Show credential pool health")
    p.set_defaults(func=_cmd_health)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "generate-key":
        print(generate_secret_key())
        return 0
    return asyncio.run(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
