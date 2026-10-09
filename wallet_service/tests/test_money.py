from decimal import Decimal

import pytest

from app.money import credits_price, net_after_fee, parse_amount, provider_fee


@pytest.mark.parametrize(
    ("paid", "fee", "credited"),
    [
        ("10.00", "0.15", "9.85"),
        ("0.99", "0.01", "0.98"),
        ("33.33", "0.50", "32.83"),
        # extra edge cases
        ("0.01", "0.00", "0.01"),
        ("0.34", "0.01", "0.33"),  # 0.0051 -> 0.01 (half-up)
        ("1.00", "0.02", "0.98"),  # 0.015 -> 0.02, exactly on the half
    ],
)
def test_provider_fee_table(paid: str, fee: str, credited: str) -> None:
    assert provider_fee(Decimal(paid)) == Decimal(fee)
    assert net_after_fee(Decimal(paid)) == Decimal(credited)


@pytest.mark.parametrize(
    ("credits", "price"),
    [
        (333, "33.30"),
        (507, "48.17"),
        (1005, "87.94"),
        # tier boundaries
        (1, "0.10"),
        (499, "49.90"),
        (500, "47.50"),
        (999, "94.91"),  # 99.9 * 0.95 = 94.905 -> 94.91
        (1000, "87.50"),
    ],
)
def test_credit_price_table(credits: int, price: str) -> None:
    assert credits_price(credits) == Decimal(price)


@pytest.mark.parametrize("value", ["10", "10.5", "10.50", "0.01", "0"])
def test_parse_amount_accepts(value: str) -> None:
    assert parse_amount(value) == Decimal(value)


@pytest.mark.parametrize(
    "value",
    [10, 10.5, "10.001", "-1.00", "1e3", " 1.00", "1,00", "", "NaN", "Infinity", None, True],
)
def test_parse_amount_rejects(value: object) -> None:
    with pytest.raises(ValueError):
        parse_amount(value)


async def test_api_rejects_number_and_three_decimals(client, factory) -> None:
    _, headers = await factory.account()
    for bad in (10, 10.0, "10.001", "abc"):
        r = await client.post("/v1/deposits", json={"amount": bad}, headers=headers)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "validation_error"


async def test_api_error_envelope_for_malformed_json(client, factory) -> None:
    _, headers = await factory.account()
    r = await client.post(
        "/v1/deposits",
        content=b"{not json",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert set(r.json()["error"]) == {"code", "message", "details"}
