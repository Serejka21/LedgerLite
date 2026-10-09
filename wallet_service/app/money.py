"""Money rules. Everything here is pure and covered by table tests."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

CENT = Decimal("0.01")
PROVIDER_FEE_RATE = Decimal("0.015")
CREDIT_BASE_PRICE = Decimal("0.10")
# (min_credits, discount) — checked from the largest tier down
CREDIT_DISCOUNT_TIERS: tuple[tuple[int, Decimal], ...] = (
    (1000, Decimal("0.125")),
    (500, Decimal("0.05")),
)
MAX_AMOUNT = Decimal("1000000000.00")  # sanity bound, well within NUMERIC(18, 2)

_AMOUNT_RE = re.compile(r"^\d{1,13}(\.\d{1,2})?$")


def quantize_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def provider_fee(paid: Decimal) -> Decimal:
    return quantize_cents(paid * PROVIDER_FEE_RATE)


def net_after_fee(paid: Decimal) -> Decimal:
    return paid - provider_fee(paid)


def credit_discount(credits: int) -> Decimal:
    for threshold, discount in CREDIT_DISCOUNT_TIERS:
        if credits >= threshold:
            return discount
    return Decimal("0")


def credits_price(credits: int) -> Decimal:
    if credits <= 0:
        raise ValueError("credits must be positive")
    base = CREDIT_BASE_PRICE * credits
    return quantize_cents(base * (Decimal("1") - credit_discount(credits)))


def parse_amount(value: object) -> Decimal:
    """Strict parser for amounts coming from JSON.

    Only strings like "10", "10.5", "10.00" are accepted. JSON numbers, exponents,
    signs, whitespace and more than two decimals are rejected.
    """
    if not isinstance(value, str):
        raise ValueError('amount must be a string, e.g. "10.00"')
    if not _AMOUNT_RE.fullmatch(value):
        raise ValueError("amount must be a non-negative decimal with at most 2 decimal places")
    amount = Decimal(value)
    if amount > MAX_AMOUNT:
        raise ValueError("amount is too large")
    return quantize_cents(amount)


def format_amount(value: Decimal) -> str:
    return f"{quantize_cents(value):.2f}"


# Pydantic type: string in, Decimal inside, string out.
MoneyStr = Annotated[
    Decimal,
    BeforeValidator(parse_amount),
    PlainSerializer(format_amount, return_type=str),
]
