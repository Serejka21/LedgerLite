import asyncio
import time
from decimal import Decimal

import pytest

from app.errors import AppError
from app.provider_client import ProviderError
from app.services.webhooks import compute_signature, verify_signature
from tests.conftest import signed_webhook

WEBHOOK_URL = "/v1/webhooks/provider"


async def _deposit(client, headers, amount="10.00") -> dict:
    r = await client.post("/v1/deposits", json={"amount": amount}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _event(
    invoice_id: str, status: str = "paid", paid: str = "10.00", event_id: str = "evt_1"
) -> dict:
    return {
        "event_id": event_id,
        "invoice_id": invoice_id,
        "status": status,
        "paid_amount": paid,
        "occurred_at": "2026-10-09T12:00:00+00:00",
    }


async def _send(client, payload: dict, **kw):
    raw, headers = signed_webhook(payload, **kw)
    return await client.post(WEBHOOK_URL, content=raw, headers=headers)


# --- signature (unit) -----------------------------------------------------------------


def test_signature_valid() -> None:
    raw, ts = b'{"a":1}', "1700000000"
    verify_signature(
        secret="s",
        raw_body=raw,
        signature=compute_signature("s", ts, raw),
        timestamp=ts,
        tolerance_seconds=300,
        now=1700000100,
    )


@pytest.mark.parametrize(
    ("signature", "timestamp", "now", "code"),
    [
        ("sha256=deadbeef", "1700000000", 1700000000, "invalid_signature"),  # wrong
        (None, "1700000000", 1700000000, "invalid_signature"),  # missing
        ("VALID", "1700000000", 1700000301, "stale_timestamp"),  # 5 min + 1 s old
        ("VALID", "1700000301", 1700000000, "stale_timestamp"),  # from the future
        ("VALID", "not-a-number", 1700000000, "invalid_signature"),
    ],
)
def test_signature_rejected(signature, timestamp, now, code) -> None:
    raw = b'{"a":1}'
    if signature == "VALID":
        signature = compute_signature("s", timestamp, raw)
    with pytest.raises(AppError) as exc:
        verify_signature(
            secret="s",
            raw_body=raw,
            signature=signature,
            timestamp=timestamp,
            tolerance_seconds=300,
            now=now,
        )
    assert exc.value.status_code == 401
    assert exc.value.code == code


def test_signature_bound_to_raw_body() -> None:
    ts = "1700000000"
    sig = compute_signature("s", ts, b'{"a":1}')
    with pytest.raises(AppError):
        verify_signature(
            secret="s",
            raw_body=b'{"a": 1}',
            signature=sig,
            timestamp=ts,
            tolerance_seconds=300,
            now=1700000000,
        )


# --- signature (API) ------------------------------------------------------------------


async def test_webhook_wrong_signature_401(client, factory) -> None:
    _, h = await factory.account()
    dep = await _deposit(client, h)
    r = await _send(client, _event(dep["invoice_id"]), secret="wrong-secret")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_signature"


async def test_webhook_expired_timestamp_401(client, factory) -> None:
    acc, h = await factory.account()
    dep = await _deposit(client, h)
    r = await _send(client, _event(dep["invoice_id"]), timestamp=int(time.time()) - 301)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "stale_timestamp"
    assert await factory.balance(acc.id) == Decimal("0.00")


async def test_webhook_tampered_body_401(client, factory) -> None:
    _, h = await factory.account()
    dep = await _deposit(client, h)
    raw, headers = signed_webhook(_event(dep["invoice_id"], paid="1.00"))
    tampered = raw.replace(b'"1.00"', b'"999.00"')
    r = await client.post(WEBHOOK_URL, content=tampered, headers=headers)
    assert r.status_code == 401


# --- deposit lifecycle ------------------------------------------------------------------


async def test_create_deposit_pending(client, factory, provider) -> None:
    _acc, h = await factory.account()
    dep = await _deposit(client, h, "25.50")
    assert dep["status"] == "pending"
    assert dep["pay_url"].startswith("http://pay/")
    assert provider.created[0]["external_id"] == dep["deposit_id"]
    assert provider.created[0]["callback_url"] == "http://wallet.test/v1/webhooks/provider"
    r = await client.get(f"/v1/deposits/{dep['deposit_id']}", headers=h)
    assert r.json()["status"] == "pending"


@pytest.mark.parametrize(
    ("kind", "status_code", "code"),
    [
        ("unavailable", 503, "provider_unavailable"),
        ("timeout", 504, "provider_timeout"),
        ("rejected", 502, "provider_error"),
    ],
)
async def test_provider_failure(client, factory, provider, kind, status_code, code) -> None:
    _, h = await factory.account()
    provider.fail_with = ProviderError(kind, "boom")
    r = await client.post("/v1/deposits", json={"amount": "10.00"}, headers=h)
    assert r.status_code == status_code
    err = r.json()["error"]
    assert err["code"] == code
    dep = (await client.get(f"/v1/deposits/{err['details']['deposit_id']}", headers=h)).json()
    assert dep["status"] == "failed"


async def test_paid_webhook_credits_net_of_fee(client, factory) -> None:
    _acc, h = await factory.account()
    dep = await _deposit(client, h, "33.33")
    r = await _send(client, _event(dep["invoice_id"], paid="33.33"))
    assert r.status_code == 200
    assert r.json()["outcome"] == "credited"
    me = (await client.get("/v1/accounts/me", headers=h)).json()
    assert me["balance_usd"] == "32.83"
    d = (await client.get(f"/v1/deposits/{dep['deposit_id']}", headers=h)).json()
    assert (d["status"], d["paid_amount"], d["fee"], d["credited_amount"]) == (
        "paid",
        "33.33",
        "0.50",
        "32.83",
    )


async def test_partially_paid_credits_actual_amount(client, factory) -> None:
    acc, h = await factory.account()
    dep = await _deposit(client, h, "10.00")
    await _send(client, _event(dep["invoice_id"], status="partially_paid", paid="7.50"))
    # 7.50 * 1.5% = 0.1125 -> 0.11
    assert await factory.balance(acc.id) == Decimal("7.39")


async def test_duplicate_webhook_not_credited_twice(client, factory) -> None:
    acc, h = await factory.account()
    dep = await _deposit(client, h)
    first = await _send(client, _event(dep["invoice_id"]))
    second = await _send(client, _event(dep["invoice_id"]))
    assert first.json()["outcome"] == "credited"
    assert second.status_code == 200
    assert second.json()["outcome"] == "duplicate"
    assert await factory.balance(acc.id) == Decimal("9.85")


async def test_concurrent_duplicate_webhooks(client, factory) -> None:
    acc, h = await factory.account()
    dep = await _deposit(client, h)
    results = await asyncio.gather(*[_send(client, _event(dep["invoice_id"])) for _ in range(10)])
    outcomes = sorted(r.json()["outcome"] for r in results)
    assert outcomes == ["credited"] + ["duplicate"] * 9
    assert await factory.balance(acc.id) == Decimal("9.85")


async def test_expired_closes_without_credit(client, factory) -> None:
    acc, h = await factory.account()
    dep = await _deposit(client, h)
    r = await _send(client, _event(dep["invoice_id"], status="expired", paid="0.00"))
    assert r.json()["outcome"] == "closed_expired"
    assert await factory.balance(acc.id) == Decimal("0.00")
    d = (await client.get(f"/v1/deposits/{dep['deposit_id']}", headers=h)).json()
    assert d["status"] == "expired"


async def test_webhook_after_close_is_ignored(client, factory) -> None:
    acc, h = await factory.account()
    dep = await _deposit(client, h)
    await _send(client, _event(dep["invoice_id"], status="expired", paid="0.00", event_id="evt_a"))
    late = await _send(client, _event(dep["invoice_id"], status="paid", event_id="evt_b"))
    assert late.status_code == 200
    assert late.json()["outcome"] == "ignored_deposit_closed"
    assert await factory.balance(acc.id) == Decimal("0.00")

    # and a second "paid" with a new event_id after a paid one does not double-credit
    dep2 = await _deposit(client, h)
    await _send(client, _event(dep2["invoice_id"], event_id="evt_c"))
    again = await _send(client, _event(dep2["invoice_id"], event_id="evt_d"))
    assert again.json()["outcome"] == "ignored_deposit_closed"
    assert await factory.balance(acc.id) == Decimal("9.85")


async def test_unknown_invoice_acknowledged(client) -> None:
    r = await _send(client, _event("inv_does_not_exist"))
    assert r.status_code == 200
    assert r.json()["outcome"] == "unknown_invoice"


async def test_invalid_webhook_payload_422(client) -> None:
    r = await _send(
        client, {"event_id": "evt_x", "invoice_id": "inv", "status": "paid", "paid_amount": 10}
    )
    assert r.status_code == 422
