"""API-key authentication.

Keys are 256-bit random tokens, so a single fast SHA-256 is sufficient for storage:
there is nothing to brute-force, unlike human passwords (which would need bcrypt/argon2).
Lookup is by hash, so the plaintext never touches the database.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models import Account

KEY_PREFIX = "ll_"


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


async def create_account(session: AsyncSession, name: str) -> tuple[Account, str]:
    key = generate_api_key()
    account = Account(id=new_id("acc"), name=name, api_key_hash=hash_api_key(key))
    session.add(account)
    await session.flush()
    return account, key


async def current_account(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> Account:
    if not x_api_key:
        raise AppError(401, "unauthorized", "Missing X-API-Key header")
    account = await session.scalar(
        select(Account).where(Account.api_key_hash == hash_api_key(x_api_key))
    )
    # End the read-only transaction so the handler can open its own explicit one.
    # (expire_on_commit=False keeps the loaded attributes usable.)
    await session.commit()
    if account is None:
        raise AppError(401, "unauthorized", "Invalid API key")
    return account


CurrentAccount = Annotated[Account, Depends(current_account)]
Session = Annotated[AsyncSession, Depends(get_session)]
