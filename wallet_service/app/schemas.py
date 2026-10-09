from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.money import MoneyStr

MIN_TRANSFER = Decimal("0.01")
MAX_CREDITS_PER_PURCHASE = 10_000_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DepositIn(StrictModel):
    amount: MoneyStr

    @field_validator("amount")
    @classmethod
    def positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be greater than 0")
        return v


class TransferIn(StrictModel):
    to_account_id: Annotated[str, Field(min_length=1, max_length=40)]
    amount: MoneyStr

    @field_validator("amount")
    @classmethod
    def at_least_one_cent(cls, v: Decimal) -> Decimal:
        if v < MIN_TRANSFER:
            raise ValueError("minimum transfer amount is 0.01")
        return v


class PurchaseIn(StrictModel):
    credits: Annotated[StrictInt, Field(gt=0, le=MAX_CREDITS_PER_PURCHASE)]


class WebhookIn(BaseModel):
    # extra fields from the provider are tolerated (forward compatibility)
    event_id: Annotated[str, Field(min_length=1, max_length=64)]
    invoice_id: Annotated[str, Field(min_length=1, max_length=64)]
    status: Literal["pending", "paid", "partially_paid", "expired"]
    paid_amount: MoneyStr
    occurred_at: str | None = None
