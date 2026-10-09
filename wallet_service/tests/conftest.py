"""Tests run against a real PostgreSQL (locking, ON CONFLICT and CHECK constraints are the
whole point, so SQLite would prove nothing). Set TEST_DATABASE_URL to point elsewhere."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import create_account
from app.config import Settings
from app.main import create_app
from app.models import Account
from app.provider_client import InvoiceCreated, InvoiceState, ProviderError
from app.services.webhooks import compute_signature

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/wallet_test"
)
WEBHOOK_SECRET = "test-webhook-secret"
ROOT = Path(__file__).resolve().parent.parent


class FakeProvider:
    """Stands in for ProviderClient; tests can make it fail or set invoice states."""

    def __init__(self) -> None:
        self.fail_with: ProviderError | None = None
        self.created: list[dict] = []
        self.states: dict[str, InvoiceState] = {}
        self._n = 0

    async def create_invoice(self, amount, external_id, callback_url) -> InvoiceCreated:
        if self.fail_with:
            raise self.fail_with
        self._n += 1
        invoice_id = f"inv_test_{self._n}_{external_id[-6:]}"
        self.created.append(
            {
                "amount": amount,
                "external_id": external_id,
                "callback_url": callback_url,
                "invoice_id": invoice_id,
            }
        )
        return InvoiceCreated(invoice_id=invoice_id, pay_url=f"http://pay/{invoice_id}")

    async def get_invoice(self, invoice_id: str) -> InvoiceState:
        if self.fail_with:
            raise self.fail_with
        return self.states.get(invoice_id, InvoiceState(invoice_id, "pending", "0.00"))

    async def aclose(self) -> None:
        pass


def make_settings(**overrides) -> Settings:
    base = dict(
        database_url=TEST_DATABASE_URL,
        provider_webhook_secret=WEBHOOK_SECRET,
        wallet_public_url="http://wallet.test",
        rate_limit_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture(scope="session")
async def app(provider: FakeProvider) -> AsyncIterator:
    application = create_app(make_settings(), provider=provider)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(scope="session")
def sessionmaker(app) -> async_sessionmaker[AsyncSession]:
    return app.state.sessionmaker


@pytest.fixture(autouse=True)
async def clean_db(sessionmaker, provider: FakeProvider) -> None:
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "TRUNCATE ledger_entries, idempotency_keys, transfers, purchases, "
                "webhook_events, deposits, accounts CASCADE"
            )
        )
    provider.fail_with = None
    provider.created.clear()
    provider.states.clear()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://wallet.test") as c:
        yield c


class Factory:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def account(
        self, name: str = "user", balance: str = "0.00", credits: int = 0
    ) -> tuple[Account, dict[str, str]]:
        async with self.sessionmaker() as s, s.begin():
            account, key = await create_account(s, name)
            account.balance_usd = Decimal(balance)
            account.balance_credits = credits
        return account, {"X-API-Key": key}

    async def balance(self, account_id: str) -> Decimal:
        async with self.sessionmaker() as s:
            return (await s.get(Account, account_id)).balance_usd


@pytest.fixture
def factory(sessionmaker) -> Factory:
    return Factory(sessionmaker)


def signed_webhook(
    payload: dict, *, secret: str = WEBHOOK_SECRET, timestamp: int | None = None
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return raw, {
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": compute_signature(secret, ts, raw),
    }
