from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import new_id
from app.errors import AppError
from app.models import Purchase
from app.money import credit_discount, credits_price, format_amount
from app.services.ledger import add_entry, lock_accounts, utcnow


async def execute_purchase(
    session: AsyncSession, account_id: str, credits: int
) -> tuple[int, dict[str, Any]]:
    """Runs inside the caller's transaction."""
    price = credits_price(credits)
    account = (await lock_accounts(session, account_id))[account_id]
    if account.balance_usd < price:
        raise AppError(
            409,
            "insufficient_funds",
            "Insufficient funds",
            {"balance_usd": format_amount(account.balance_usd), "required": format_amount(price)},
        )

    now = utcnow()
    purchase = Purchase(
        id=new_id("pur"), account_id=account.id, credits=credits, price_usd=price, created_at=now
    )
    session.add(purchase)
    account.balance_usd = account.balance_usd - price
    account.balance_credits = account.balance_credits + credits
    add_entry(
        session,
        account,
        type_="purchase",
        usd_delta=-price,
        credits_delta=credits,
        reference_id=purchase.id,
        details={"discount": str(credit_discount(credits))},
        created_at=now,
    )
    await session.flush()

    return 201, {
        "purchase_id": purchase.id,
        "credits": credits,
        "price_usd": format_amount(price),
        "balance_usd": format_amount(account.balance_usd),
        "balance_credits": account.balance_credits,
        "created_at": now.isoformat(),
    }
