from __future__ import annotations

import hashlib
import hmac
import time

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import WebhookEvent
from app.schemas import WebhookIn
from app.services.deposits import apply_provider_status, lock_deposit_by_invoice

SIGNATURE_PREFIX = "sha256="


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = timestamp.encode() + b"." + raw_body
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> None:
    """Raises AppError(401) unless the signature is valid and the timestamp is fresh."""
    if not signature or not timestamp:
        raise AppError(401, "invalid_signature", "Missing X-Signature or X-Timestamp header")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise AppError(401, "invalid_signature", "X-Timestamp must be a unix timestamp") from exc

    now = time.time() if now is None else now
    if abs(now - ts) > tolerance_seconds:
        raise AppError(
            401,
            "stale_timestamp",
            "Webhook timestamp is outside the allowed window",
            {"tolerance_seconds": tolerance_seconds},
        )

    expected = compute_signature(secret, timestamp, raw_body)
    # Constant-time comparison; signature is checked over the *raw* bytes, never re-serialized.
    if not hmac.compare_digest(expected.encode(), signature.strip().encode()):
        raise AppError(401, "invalid_signature", "Webhook signature mismatch")


def parse_webhook(raw_body: bytes) -> WebhookIn:
    try:
        return WebhookIn.model_validate_json(raw_body)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        if any(e["type"] == "json_invalid" for e in errors):
            raise AppError(400, "malformed_json", "Webhook body is not valid JSON") from exc
        raise AppError(
            422, "validation_error", "Invalid webhook payload", {"errors": errors}
        ) from exc


async def process_webhook(session: AsyncSession, event: WebhookIn) -> str:
    """Idempotent processing of a verified provider event. Returns the outcome."""
    async with session.begin():
        # Dedup: the event_id primary key makes the second delivery a no-op. If two copies
        # arrive concurrently, the second INSERT waits for the first transaction and then
        # hits the conflict.
        inserted = await session.scalar(
            pg_insert(WebhookEvent)
            .values(
                event_id=event.event_id,
                invoice_id=event.invoice_id,
                status=event.status,
                paid_amount=event.paid_amount,
                outcome="processing",
                payload=event.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(WebhookEvent.event_id)
        )
        if inserted is None:
            return "duplicate"

        dep = await lock_deposit_by_invoice(session, event.invoice_id)
        if dep is None:
            outcome = "unknown_invoice"
        else:
            outcome = await apply_provider_status(
                session,
                dep,
                event.status,
                event.paid_amount,
                source="webhook",
                event_id=event.event_id,
            )

        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.event_id == event.event_id)
            .values(outcome=outcome)
        )
    return outcome
