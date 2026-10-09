from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import new_id
from app.errors import AppError
from app.models import Account, Deposit
from app.money import format_amount, net_after_fee, parse_amount, provider_fee
from app.provider_client import ProviderClient, ProviderError
from app.services.ledger import add_entry, lock_accounts, utcnow

log = logging.getLogger(__name__)

FINAL_PROVIDER_STATUSES = {"paid", "partially_paid", "expired"}

_PROVIDER_ERROR_HTTP = {
    "timeout": (504, "provider_timeout"),
    "unavailable": (503, "provider_unavailable"),
    "rejected": (502, "provider_error"),
    "bad_response": (502, "provider_error"),
}


def deposit_view(dep: Deposit) -> dict:
    def money(v: Decimal | None) -> str | None:
        return None if v is None else format_amount(v)

    return {
        "deposit_id": dep.id,
        "status": dep.status,
        "amount": format_amount(dep.amount),
        "pay_url": dep.pay_url,
        "invoice_id": dep.invoice_id,
        "paid_amount": money(dep.paid_amount),
        "fee": money(dep.fee),
        "credited_amount": money(dep.credited_amount),
        "failure_reason": dep.failure_reason,
        "created_at": dep.created_at.isoformat() if dep.created_at else None,
        "closed_at": dep.closed_at.isoformat() if dep.closed_at else None,
    }


async def create_deposit(
    session: AsyncSession,
    account: Account,
    amount: Decimal,
    provider: ProviderClient,
    callback_url: str,
) -> Deposit:
    # 1. Persist the deposit first, so the provider's external_id always points to a real row.
    dep = Deposit(
        id=new_id("dep"),
        account_id=account.id,
        amount=amount,
        status="pending",
        created_at=utcnow(),
    )
    session.add(dep)
    await session.commit()

    # 2. Call the provider outside of any DB transaction (no locks held during network I/O).
    try:
        invoice = await provider.create_invoice(
            amount, external_id=dep.id, callback_url=callback_url
        )
    except ProviderError as exc:
        dep.status = "failed"
        dep.failure_reason = f"{exc.kind}: {exc.message}"[:200]
        dep.closed_at = utcnow()
        await session.commit()
        status_code, code = _PROVIDER_ERROR_HTTP[exc.kind]
        raise AppError(status_code, code, exc.message, {"deposit_id": dep.id}) from exc

    dep.invoice_id = invoice.invoice_id
    dep.pay_url = invoice.pay_url
    await session.commit()
    return dep


async def get_deposit(session: AsyncSession, account: Account, deposit_id: str) -> Deposit:
    dep = await session.scalar(
        select(Deposit).where(Deposit.id == deposit_id, Deposit.account_id == account.id)
    )
    if dep is None:
        raise AppError(404, "deposit_not_found", "Deposit not found", {"deposit_id": deposit_id})
    return dep


async def apply_provider_status(
    session: AsyncSession,
    dep: Deposit,
    status: str,
    paid_amount: Decimal,
    *,
    source: str,
    event_id: str | None = None,
) -> str:
    """Move a *locked* deposit to the provider's final state. Returns the outcome.

    Caller must hold `SELECT ... FOR UPDATE` on the deposit row and own the transaction.
    Only a pending deposit can change; anything that arrives later is ignored.
    """
    if dep.status != "pending":
        return "ignored_deposit_closed"
    if status not in FINAL_PROVIDER_STATUSES:
        return "ignored_not_final"

    now = utcnow()
    if status == "expired":
        dep.status = "expired"
        dep.paid_amount = Decimal("0.00")
        dep.fee = Decimal("0.00")
        dep.credited_amount = Decimal("0.00")
        dep.closed_at = now
        return "closed_expired"

    fee = provider_fee(paid_amount)
    net = net_after_fee(paid_amount)
    accounts = await lock_accounts(session, dep.account_id)
    account = accounts[dep.account_id]
    account.balance_usd = account.balance_usd + net

    dep.status = status
    dep.paid_amount = paid_amount
    dep.fee = fee
    dep.credited_amount = net
    dep.closed_at = now

    add_entry(
        session,
        account,
        type_="deposit",
        usd_delta=net,
        reference_id=dep.id,
        details={
            "invoice_id": dep.invoice_id,
            "provider_status": status,
            "paid_amount": format_amount(paid_amount),
            "fee": format_amount(fee),
            "source": source,
            "event_id": event_id,
        },
        created_at=now,
    )
    log.info(
        "deposit %s %s: paid=%s fee=%s credited=%s (source=%s)",
        dep.id,
        status,
        paid_amount,
        fee,
        net,
        source,
    )
    return "credited"


async def lock_deposit_by_invoice(session: AsyncSession, invoice_id: str) -> Deposit | None:
    return await session.scalar(
        select(Deposit)
        .where(Deposit.invoice_id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def parse_provider_amount(value: str) -> Decimal:
    try:
        return parse_amount(value)
    except ValueError as exc:
        raise AppError(
            422, "validation_error", f"Invalid paid_amount from provider: {exc}"
        ) from exc
