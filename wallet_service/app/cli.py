"""Operational commands.

python -m app.cli create-account --name alice
python -m app.cli seed                      # alice + bob, prints their API keys
python -m app.cli reconcile --older-than-minutes 15
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.auth import create_account
from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.provider_client import ProviderClient
from app.services.reconcile import reconcile_pending_deposits


async def _create_accounts(names: list[str]) -> None:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            for name in names:
                account, key = await create_account(session, name)
                # The key is printed once and never stored in plaintext.
                print(json.dumps({"name": name, "account_id": account.id, "api_key": key}))
    finally:
        await engine.dispose()


async def _reconcile(older_than_minutes: int) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    provider = ProviderClient(settings)
    try:
        stats = await reconcile_pending_deposits(
            make_sessionmaker(engine), provider, older_than_minutes=older_than_minutes
        )
        print(json.dumps(stats))
    finally:
        await provider.aclose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="wallet")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create-account", help="create an account and print its API key")
    p_create.add_argument("--name", required=True)
    sub.add_parser("seed", help="create demo accounts alice and bob")
    p_rec = sub.add_parser("reconcile", help="settle pending deposits from provider state")
    p_rec.add_argument("--older-than-minutes", type=int, default=15)
    args = parser.parse_args()

    if args.cmd == "create-account":
        asyncio.run(_create_accounts([args.name]))
    elif args.cmd == "seed":
        asyncio.run(_create_accounts(["alice", "bob"]))
    elif args.cmd == "reconcile":
        asyncio.run(_reconcile(args.older_than_minutes))


if __name__ == "__main__":
    main()
