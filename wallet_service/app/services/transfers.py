from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import new_id
from app.errors import AppError
from app.models import Transfer
from app.money import format_amount
from app.services.ledger import add_entry, lock_accounts, utcnow


async def execute_transfer(
    session: AsyncSession, from_account_id: str, to_account_id: str, amount: Decimal
) -> tuple[int, dict[str, Any]]:
    """Runs inside the caller's transaction."""
    if from_account_id == to_account_id:
        raise AppError(422, "self_transfer", "Cannot transfer to the same account")

    accounts = await lock_accounts(session, from_account_id, to_account_id)
    if to_account_id not in accounts:
        raise AppError(
            404,
            "account_not_found",
            "Recipient account not found",
            {"to_account_id": to_account_id},
        )
    src, dst = accounts[from_account_id], accounts[to_account_id]

    # Check under the row lock; the DB CHECK (balance_usd >= 0) is a second safety net.
    if src.balance_usd < amount:
        raise AppError(
            409,
            "insufficient_funds",
            "Insufficient funds",
            {"balance_usd": format_amount(src.balance_usd), "required": format_amount(amount)},
        )

    now = utcnow()
    transfer = Transfer(
        id=new_id("trf"),
        from_account_id=src.id,
        to_account_id=dst.id,
        amount=amount,
        created_at=now,
    )
    session.add(transfer)
    src.balance_usd = src.balance_usd - amount
    dst.balance_usd = dst.balance_usd + amount
    add_entry(
        session,
        src,
        type_="transfer_out",
        usd_delta=-amount,
        reference_id=transfer.id,
        counterparty_account_id=dst.id,
        created_at=now,
    )
    add_entry(
        session,
        dst,
        type_="transfer_in",
        usd_delta=amount,
        reference_id=transfer.id,
        counterparty_account_id=src.id,
        created_at=now,
    )
    await session.flush()

    return 201, {
        "transfer_id": transfer.id,
        "from_account_id": src.id,
        "to_account_id": dst.id,
        "amount": format_amount(amount),
        "balance_usd": format_amount(src.balance_usd),
        "created_at": now.isoformat(),
    }
