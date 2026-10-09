from decimal import Decimal

import pytest


async def _buy(client, headers, credits, key=None):
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return await client.post("/v1/purchases", json={"credits": credits}, headers=h)


@pytest.mark.parametrize(
    ("credits", "price", "left"),
    [
        (333, "33.30", "66.70"),
        (507, "48.17", "51.83"),
        (1005, "87.94", "12.06"),
    ],
)
async def test_purchase_uses_price_table(client, factory, credits, price, left) -> None:
    acc, h = await factory.account("alice", "100.00")
    r = await _buy(client, h, credits)
    assert r.status_code == 201, r.text
    assert r.json()["price_usd"] == price
    me = (await client.get("/v1/accounts/me", headers=h)).json()
    assert me == {
        "account_id": acc.id,
        "name": "alice",
        "balance_usd": left,
        "balance_credits": credits,
    }


async def test_purchase_insufficient_funds(client, factory) -> None:
    acc, h = await factory.account("alice", "48.16")
    r = await _buy(client, h, 507)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "insufficient_funds"
    assert await factory.balance(acc.id) == Decimal("48.16")


@pytest.mark.parametrize("bad", [0, -5, "507", 5.5, True, None])
async def test_purchase_validation(client, factory, bad) -> None:
    _, h = await factory.account("alice", "100.00")
    r = await _buy(client, h, bad)
    assert r.status_code == 422


async def test_purchase_optional_idempotency(client, factory) -> None:
    acc, h = await factory.account("alice", "100.00")
    await _buy(client, h, 10, key="p1")
    r = await _buy(client, h, 10, key="p1")
    assert r.headers["Idempotent-Replayed"] == "true"
    assert await factory.balance(acc.id) == Decimal("99.00")


async def test_transactions_history_and_pagination(client, factory) -> None:
    alice, ha = await factory.account("alice", "100.00")
    bob, hb = await factory.account("bob")
    for i in range(5):
        r = await client.post(
            "/v1/transfers",
            json={"to_account_id": bob.id, "amount": "1.00"},
            headers={**ha, "Idempotency-Key": f"t{i}"},
        )
        assert r.status_code == 201
    await _buy(client, ha, 10)

    page1 = (await client.get("/v1/transactions?limit=4", headers=ha)).json()
    assert [i["type"] for i in page1["items"]] == ["purchase"] + ["transfer_out"] * 3
    assert page1["items"][0]["amount_usd"] == "-1.00"
    assert page1["items"][0]["credits"] == 10
    assert page1["items"][0]["balance_usd_after"] == "94.00"
    assert page1["next_cursor"] is not None

    page2 = (
        await client.get(f"/v1/transactions?limit=4&cursor={page1['next_cursor']}", headers=ha)
    ).json()
    assert len(page2["items"]) == 2
    assert page2["next_cursor"] is None
    ids = [i["id"] for i in page1["items"] + page2["items"]]
    assert len(set(ids)) == 6

    # recipient sees only its own side
    bob_items = (await client.get("/v1/transactions", headers=hb)).json()["items"]
    assert {i["type"] for i in bob_items} == {"transfer_in"}
    assert all(i["counterparty_account_id"] == alice.id for i in bob_items)


async def test_transactions_bad_params(client, factory) -> None:
    _, h = await factory.account()
    assert (await client.get("/v1/transactions?limit=0", headers=h)).status_code == 422
    assert (await client.get("/v1/transactions?limit=101", headers=h)).status_code == 422
    r = await client.get("/v1/transactions?cursor=abc", headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_cursor"


async def test_unknown_route_uses_error_envelope(client) -> None:
    r = await client.get("/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
