from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, LedgerEntry


def utcnow() -> datetime:
    return datetime.now(UTC)


async def lock_accounts(session: AsyncSession, *account_ids: str) -> dict[str, Account]:
    """Row-lock accounts in a stable (sorted) order so that two concurrent
    transfers A->B and B->A cannot deadlock.

    FOR NO KEY UPDATE rather than FOR UPDATE: inserts into tables with a FK to
    accounts (idempotency_keys, transfers, ledger_entries) take FOR KEY SHARE on the
    account row. FOR UPDATE conflicts with KEY SHARE, so two requests of the same
    user that had both inserted their idempotency key would deadlock each other.
    NO KEY UPDATE is exactly what a balance UPDATE needs and does not conflict.
    """
    ids = sorted(set(account_ids))
    rows = await session.scalars(
        select(Account)
        .where(Account.id.in_(ids))
        .order_by(Account.id)
        .with_for_update(key_share=True)  # renders FOR NO KEY UPDATE
        .execution_options(populate_existing=True)
    )
    return {a.id: a for a in rows}


def add_entry(
    session: AsyncSession,
    account: Account,
    *,
    type_: str,
    usd_delta: Decimal = Decimal("0.00"),
    credits_delta: int = 0,
    reference_id: str,
    counterparty_account_id: str | None = None,
    details: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> LedgerEntry:
    """Must be called after the account balance has been updated."""
    entry = LedgerEntry(
        account_id=account.id,
        type=type_,
        usd_delta=usd_delta,
        credits_delta=credits_delta,
        balance_usd_after=account.balance_usd,
        balance_credits_after=account.balance_credits,
        reference_id=reference_id,
        counterparty_account_id=counterparty_account_id,
        details=details or {},
        created_at=created_at or utcnow(),
    )
    session.add(entry)
    return entry
