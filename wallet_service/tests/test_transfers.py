import asyncio
from decimal import Decimal

import pytest


async def _transfer(client, headers, to: str, amount: str, key: str | None = "k1"):
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    return await client.post(
        "/v1/transfers", json={"to_account_id": to, "amount": amount}, headers=h
    )


async def test_transfer_moves_money(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    r = await _transfer(client, ha, bob.id, "4.25")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["amount"] == "4.25"
    assert body["balance_usd"] == "5.75"
    assert r.headers["Idempotent-Replayed"] == "false"
    assert await factory.balance(alice.id) == Decimal("5.75")
    assert await factory.balance(bob.id) == Decimal("4.25")


async def test_missing_idempotency_key_400(client, factory) -> None:
    _, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    r = await _transfer(client, ha, bob.id, "1.00", key=None)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_idempotency_key"


async def test_same_key_same_body_replays_without_second_debit(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    first = await _transfer(client, ha, bob.id, "3.00", key="same")
    second = await _transfer(client, ha, bob.id, "3.00", key="same")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert await factory.balance(alice.id) == Decimal("7.00")
    assert await factory.balance(bob.id) == Decimal("3.00")


async def test_equivalent_amount_formatting_is_same_request(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    await _transfer(client, ha, bob.id, "3.0", key="fmt")
    r = await _transfer(client, ha, bob.id, "3.00", key="fmt")
    assert r.status_code == 201
    assert r.headers["Idempotent-Replayed"] == "true"
    assert await factory.balance(alice.id) == Decimal("7.00")


async def test_same_key_different_body_409(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    await _transfer(client, ha, bob.id, "3.00", key="dup")
    r = await _transfer(client, ha, bob.id, "4.00", key="dup")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_key_reused"
    assert await factory.balance(alice.id) == Decimal("7.00")


async def test_keys_are_scoped_per_account(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    bob, hb = await factory.account("bob", "10.00")
    r1 = await _transfer(client, ha, bob.id, "1.00", key="shared")
    r2 = await _transfer(client, hb, alice.id, "2.00", key="shared")
    assert r1.status_code == r2.status_code == 201
    assert r2.headers["Idempotent-Replayed"] == "false"


async def test_insufficient_funds(client, factory) -> None:
    alice, ha = await factory.account("alice", "5.00")
    bob, _ = await factory.account("bob")
    r = await _transfer(client, ha, bob.id, "5.01")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "insufficient_funds"
    assert err["details"] == {"balance_usd": "5.00", "required": "5.01"}
    assert await factory.balance(alice.id) == Decimal("5.00")


async def test_failed_attempt_does_not_burn_the_key(client, factory, sessionmaker) -> None:
    from app.models import Account

    alice, ha = await factory.account("alice", "1.00")
    bob, _ = await factory.account("bob")
    assert (await _transfer(client, ha, bob.id, "2.00", key="retry")).status_code == 409
    async with sessionmaker() as s, s.begin():
        (await s.get(Account, alice.id)).balance_usd = Decimal("2.00")
    r = await _transfer(client, ha, bob.id, "2.00", key="retry")
    assert r.status_code == 201


@pytest.mark.parametrize("amount", ["0.00", "0", "-1.00", "0.001"])
async def test_amount_below_minimum_422(client, factory, amount) -> None:
    _, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    r = await _transfer(client, ha, bob.id, amount)
    assert r.status_code == 422


async def test_minimum_amount_one_cent(client, factory) -> None:
    _, ha = await factory.account("alice", "0.01")
    bob, _ = await factory.account("bob")
    assert (await _transfer(client, ha, bob.id, "0.01")).status_code == 201


async def test_self_transfer_422(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    r = await _transfer(client, ha, alice.id, "1.00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "self_transfer"


async def test_unknown_recipient_404(client, factory) -> None:
    _, ha = await factory.account("alice", "10.00")
    r = await _transfer(client, ha, "acc_nope", "1.00")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "account_not_found"


async def test_auth_required(client, factory) -> None:
    bob, _ = await factory.account("bob")
    r = await _transfer(client, {"X-API-Key": "wrong"}, bob.id, "1.00")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_concurrent_transfers_never_overdraw(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    results = await asyncio.gather(
        *[_transfer(client, ha, bob.id, "1.00", key=f"c{i}") for i in range(25)]
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 10
    assert codes.count(409) == 15
    assert await factory.balance(alice.id) == Decimal("0.00")
    assert await factory.balance(bob.id) == Decimal("10.00")


async def test_concurrent_same_key_debits_once(client, factory) -> None:
    alice, ha = await factory.account("alice", "10.00")
    bob, _ = await factory.account("bob")
    results = await asyncio.gather(
        *[_transfer(client, ha, bob.id, "1.00", key="one") for _ in range(10)]
    )
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()["transfer_id"] for r in results}) == 1
    assert await factory.balance(alice.id) == Decimal("9.00")


async def test_opposite_direction_transfers_do_not_deadlock(client, factory) -> None:
    alice, ha = await factory.account("alice", "50.00")
    bob, hb = await factory.account("bob", "50.00")
    tasks = []
    for i in range(20):
        tasks.append(_transfer(client, ha, bob.id, "1.00", key=f"ab{i}"))
        tasks.append(_transfer(client, hb, alice.id, "1.00", key=f"ba{i}"))
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
    assert all(r.status_code == 201 for r in results)
    assert await factory.balance(alice.id) == Decimal("50.00")
    assert await factory.balance(bob.id) == Decimal("50.00")
