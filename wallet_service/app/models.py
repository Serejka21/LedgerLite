from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Money = Numeric(18, 2)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("balance_usd >= 0", name="ck_accounts_balance_usd_non_negative"),
        CheckConstraint("balance_credits >= 0", name="ck_accounts_balance_credits_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # sha256 of the API key; the plaintext key is shown once at creation and never stored
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    balance_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), server_default="0")
    balance_credits: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Deposit(Base):
    __tablename__ = "deposits"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_deposits_amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'paid', 'partially_paid', 'expired', 'failed')",
            name="ck_deposits_status",
        ),
        Index("ix_deposits_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    invoice_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    pay_url: Mapped[str | None] = mapped_column(String(500))
    paid_amount: Mapped[Decimal | None] = mapped_column(Money)
    fee: Mapped[Decimal | None] = mapped_column(Money)
    credited_amount: Mapped[Decimal | None] = mapped_column(Money)
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEvent(Base):
    """Every accepted (correctly signed) provider event, keyed by event_id for dedup."""

    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20))
    paid_amount: Mapped[Decimal] = mapped_column(Money)
    outcome: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_transfers_amount_positive"),
        CheckConstraint("from_account_id <> to_account_id", name="ck_transfers_not_self"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    from_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    to_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (
        CheckConstraint("credits > 0", name="ck_purchases_credits_positive"),
        CheckConstraint("price_usd >= 0", name="ck_purchases_price_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    credits: Mapped[int] = mapped_column(BigInteger)
    price_usd: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerEntry(Base):
    """Append-only history of balance changes. One row per account per operation."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "type IN ('deposit', 'transfer_out', 'transfer_in', 'purchase')",
            name="ck_ledger_entries_type",
        ),
        Index("ix_ledger_entries_account_id_id", "account_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    type: Mapped[str] = mapped_column(String(20))
    usd_delta: Mapped[Decimal] = mapped_column(Money)
    credits_delta: Mapped[int] = mapped_column(BigInteger, default=0)
    balance_usd_after: Mapped[Decimal] = mapped_column(Money)
    balance_credits_after: Mapped[int] = mapped_column(BigInteger)
    reference_id: Mapped[str] = mapped_column(String(40))
    counterparty_account_id: Mapped[str | None] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
