"""Idempotency-Key handling.

The key row is inserted in the *same* transaction as the money movement:

* first request: INSERT succeeds -> operation runs -> response stored -> COMMIT.
* retry after commit: INSERT conflicts -> stored response is replayed, nothing is debited.
* concurrent retry: the second INSERT blocks on the uncommitted row until the first
  transaction finishes, then sees the conflict and replays.
* operation failed (e.g. insufficient funds): the whole transaction, including the key,
  rolls back, so the client may retry the same key once the problem is fixed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import IdempotencyKey

MAX_KEY_LENGTH = 255


def validate_key(key: str | None, *, required: bool) -> str | None:
    if key is None or key == "":
        if required:
            raise AppError(400, "missing_idempotency_key", "Idempotency-Key header is required")
        return None
    if len(key) > MAX_KEY_LENGTH:
        raise AppError(
            400,
            "invalid_idempotency_key",
            f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters",
        )
    return key


def request_fingerprint(endpoint: str, payload: dict[str, Any]) -> str:
    """Hash of the *validated* body, so "5.0" and "5.00" count as the same request."""
    canonical = json.dumps(
        {"endpoint": endpoint, "body": payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


Operation = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


async def run_idempotent(
    session: AsyncSession,
    *,
    account_id: str,
    key: str | None,
    endpoint: str,
    request_hash: str,
    operation: Operation,
) -> tuple[int, dict[str, Any], bool]:
    """Returns (status_code, body, replayed)."""
    async with session.begin():
        if key is None:
            status_code, body = await operation()
            return status_code, body, False

        inserted = await session.scalar(
            pg_insert(IdempotencyKey)
            .values(
                account_id=account_id,
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                response_status=0,
                response_body={},
            )
            .on_conflict_do_nothing(index_elements=["account_id", "key"])
            .returning(IdempotencyKey.key)
        )
        if inserted is None:
            existing = await session.scalar(
                select(IdempotencyKey)
                .where(IdempotencyKey.account_id == account_id, IdempotencyKey.key == key)
                .execution_options(populate_existing=True)
            )
            assert existing is not None  # conflict means a committed row exists
            if existing.endpoint != endpoint or existing.request_hash != request_hash:
                raise AppError(
                    409,
                    "idempotency_key_reused",
                    "Idempotency-Key was already used with a different request",
                    {"idempotency_key": key},
                )
            return existing.response_status, existing.response_body, True

        status_code, body = await operation()
        await session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.account_id == account_id, IdempotencyKey.key == key)
            .values(response_status=status_code, response_body=body)
        )
    return status_code, body, False
