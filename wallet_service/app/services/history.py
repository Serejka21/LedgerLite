from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import LedgerEntry
from app.money import format_amount


def _entry_view(e: LedgerEntry) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "type": e.type,
        "amount_usd": format_amount(e.usd_delta),
        "credits": e.credits_delta,
        "balance_usd_after": format_amount(e.balance_usd_after),
        "balance_credits_after": e.balance_credits_after,
        "reference_id": e.reference_id,
        "counterparty_account_id": e.counterparty_account_id,
        "details": e.details,
        "created_at": e.created_at.isoformat(),
    }


async def list_transactions(
    session: AsyncSession, account_id: str, *, limit: int, cursor: str | None
) -> dict[str, Any]:
    """Keyset pagination, newest first. `cursor` is the id of the last item already seen;
    unlike OFFSET it stays stable while new operations are being appended."""
    query = select(LedgerEntry).where(LedgerEntry.account_id == account_id)
    if cursor is not None:
        if not cursor.isdigit():
            raise AppError(400, "invalid_cursor", "cursor must come from a previous response")
        query = query.where(LedgerEntry.id < int(cursor))
    rows = list(await session.scalars(query.order_by(LedgerEntry.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_entry_view(e) for e in rows],
        "next_cursor": str(rows[-1].id) if has_more else None,
    }
