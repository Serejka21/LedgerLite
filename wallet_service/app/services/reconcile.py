"""Bonus 1: reconciliation for lost webhooks.

Finds deposits still pending after N minutes, asks the provider for the invoice state
and applies it through the same code path as the webhook (so the rules are identical
and a late webhook afterwards is ignored because the deposit is already closed).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Deposit
from app.provider_client import ProviderClient, ProviderError
from app.services.deposits import (
    FINAL_PROVIDER_STATUSES,
    apply_provider_status,
    parse_provider_amount,
)
from app.services.ledger import utcnow

log = logging.getLogger(__name__)


async def reconcile_pending_deposits(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: ProviderClient,
    *,
    older_than_minutes: int,
    batch_size: int = 100,
) -> dict[str, int]:
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    async with sessionmaker() as session:
        candidates = list(
            await session.scalars(
                select(Deposit.id)
                .where(Deposit.status == "pending", Deposit.created_at < cutoff)
                .order_by(Deposit.created_at)
                .limit(batch_size)
            )
        )

    stats: Counter[str] = Counter(checked=len(candidates))
    for deposit_id in candidates:
        async with sessionmaker() as session:
            dep = await session.get(Deposit, deposit_id)
            if dep is None or dep.status != "pending":
                stats["skipped"] += 1
                continue
            invoice_id = dep.invoice_id
            await session.rollback()

            if invoice_id is None:
                # Crash or timeout between saving the deposit and receiving the invoice.
                async with session.begin():
                    locked = await session.get(
                        Deposit, deposit_id, with_for_update=True, populate_existing=True
                    )
                    if locked is not None and locked.status == "pending":
                        locked.status = "failed"
                        locked.failure_reason = "invoice was never created"
                        locked.closed_at = utcnow()
                        stats["failed_no_invoice"] += 1
                continue

            # Network call happens outside of the DB transaction.
            try:
                state = await provider.get_invoice(invoice_id)
            except ProviderError as exc:
                log.warning("reconcile: provider error for %s: %s", deposit_id, exc.message)
                stats["provider_errors"] += 1
                continue

            if state.status not in FINAL_PROVIDER_STATUSES:
                stats["still_pending"] += 1
                continue

            async with session.begin():
                locked = await session.get(
                    Deposit, deposit_id, with_for_update=True, populate_existing=True
                )
                assert locked is not None
                outcome = await apply_provider_status(
                    session,
                    locked,
                    state.status,
                    parse_provider_amount(state.paid_amount),
                    source="reconcile",
                )
            stats[outcome] += 1
            log.info("reconcile: %s -> %s (%s)", deposit_id, state.status, outcome)
    return dict(stats)
