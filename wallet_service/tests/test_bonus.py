"""Bonus features: reconciliation of lost webhooks and per-key rate limiting."""

from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import update

from app.main import create_app
from app.models import Deposit
from app.provider_client import InvoiceState, ProviderError
from app.ratelimit import SlidingWindowLimiter
from app.services.ledger import utcnow
from app.services.reconcile import reconcile_pending_deposits
from tests.conftest import FakeProvider, make_settings, signed_webhook


async def _age_deposits(sessionmaker, minutes: int) -> None:
    async with sessionmaker() as s, s.begin():
        await s.execute(update(Deposit).values(created_at=utcnow() - timedelta(minutes=minutes)))


async def _deposit(client, h, amount="10.00") -> dict:
    r = await client.post("/v1/deposits", json={"amount": amount}, headers=h)
    return r.json()


async def test_reconcile_settles_lost_webhook(client, factory, provider, sessionmaker) -> None:
    acc, h = await factory.account()
    paid = await _deposit(client, h)
    expired = await _deposit(client, h)
    fresh = await _deposit(client, h)
    provider.states[paid["invoice_id"]] = InvoiceState(paid["invoice_id"], "paid", "10.00")
    provider.states[expired["invoice_id"]] = InvoiceState(expired["invoice_id"], "expired", "0")
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(Deposit)
            .where(Deposit.id != fresh["deposit_id"])
            .values(created_at=utcnow() - timedelta(minutes=30))
        )

    stats = await reconcile_pending_deposits(sessionmaker, provider, older_than_minutes=15)
    assert stats["checked"] == 2
    assert stats["credited"] == 1
    assert stats["closed_expired"] == 1
    assert await factory.balance(acc.id) == Decimal("9.85")

    statuses = {
        d["deposit_id"]: (await client.get(f"/v1/deposits/{d['deposit_id']}", headers=h)).json()[
            "status"
        ]
        for d in (paid, expired, fresh)
    }
    assert statuses == {
        paid["deposit_id"]: "paid",
        expired["deposit_id"]: "expired",
        fresh["deposit_id"]: "pending",
    }

    # the late webhook arriving after reconciliation must not credit again
    raw, headers = signed_webhook(
        {
            "event_id": "evt_late",
            "invoice_id": paid["invoice_id"],
            "status": "paid",
            "paid_amount": "10.00",
        }
    )
    r = await client.post("/v1/webhooks/provider", content=raw, headers=headers)
    assert r.json()["outcome"] == "ignored_deposit_closed"
    assert await factory.balance(acc.id) == Decimal("9.85")

    # running again is a no-op
    again = await reconcile_pending_deposits(sessionmaker, provider, older_than_minutes=15)
    assert again == {"checked": 0}


async def test_reconcile_survives_provider_errors(client, factory, provider, sessionmaker) -> None:
    _, h = await factory.account()
    await _deposit(client, h)
    await _age_deposits(sessionmaker, 30)
    provider.fail_with = ProviderError("timeout", "slow")
    stats = await reconcile_pending_deposits(sessionmaker, provider, older_than_minutes=15)
    assert stats == {"checked": 1, "provider_errors": 1}


def test_sliding_window_limiter() -> None:
    now = [1000.0]
    limiter = SlidingWindowLimiter(limit=3, window=60, clock=lambda: now[0])
    assert [limiter.hit("k") for _ in range(3)] == [None, None, None]
    assert limiter.hit("k") == 60.0
    assert limiter.hit("other") is None
    now[0] += 59
    assert limiter.hit("k") == 1.0
    now[0] += 1
    assert limiter.hit("k") is None


async def test_rate_limit_429_and_webhook_exempt(factory) -> None:
    app = create_app(
        make_settings(rate_limit_enabled=True, rate_limit_per_minute=3), provider=FakeProvider()
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            _, h = await factory.account("alice", "100.00")
            codes = [
                (await client.post("/v1/purchases", json={"credits": 1}, headers=h)).status_code
                for _ in range(3)
            ]
            assert codes == [201, 201, 201]
            r = await client.post("/v1/purchases", json={"credits": 1}, headers=h)
            assert r.status_code == 429
            assert r.json()["error"]["code"] == "rate_limited"
            assert 1 <= int(r.headers["Retry-After"]) <= 60

            # GET is not limited
            assert (await client.get("/v1/accounts/me", headers=h)).status_code == 200
            # another key has its own budget
            _, h2 = await factory.account("bob", "1.00")
            assert (
                await client.post("/v1/purchases", json={"credits": 1}, headers=h2)
            ).status_code == 201
            # provider webhooks are never limited
            for i in range(5):
                raw, wh = signed_webhook(
                    {
                        "event_id": f"e{i}",
                        "invoice_id": "inv_x",
                        "status": "paid",
                        "paid_amount": "1.00",
                    }
                )
                r = await client.post("/v1/webhooks/provider", content=raw, headers=wh)
                assert r.status_code == 200
