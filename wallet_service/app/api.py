from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from app.auth import CurrentAccount, Session
from app.config import Settings
from app.money import format_amount
from app.provider_client import ProviderClient
from app.schemas import DepositIn, PurchaseIn, TransferIn
from app.services import deposits as deposits_svc
from app.services.history import list_transactions
from app.services.idempotency import request_fingerprint, run_idempotent, validate_key
from app.services.purchases import execute_purchase
from app.services.transfers import execute_transfer
from app.services.webhooks import parse_webhook, process_webhook, verify_signature

router = APIRouter(prefix="/v1")

IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _provider(request: Request) -> ProviderClient:
    return request.app.state.provider


@router.get("/accounts/me")
async def get_me(account: CurrentAccount) -> dict:
    return {
        "account_id": account.id,
        "name": account.name,
        "balance_usd": format_amount(account.balance_usd),
        "balance_credits": account.balance_credits,
    }


@router.post("/deposits", status_code=201)
async def create_deposit(
    body: DepositIn, account: CurrentAccount, session: Session, request: Request
) -> dict:
    settings = _settings(request)
    callback_url = settings.wallet_public_url.rstrip("/") + "/v1/webhooks/provider"
    dep = await deposits_svc.create_deposit(
        session, account, body.amount, _provider(request), callback_url
    )
    return deposits_svc.deposit_view(dep)


@router.get("/deposits/{deposit_id}")
async def get_deposit(deposit_id: str, account: CurrentAccount, session: Session) -> dict:
    return deposits_svc.deposit_view(await deposits_svc.get_deposit(session, account, deposit_id))


@router.post("/webhooks/provider")
async def provider_webhook(request: Request, session: Session) -> dict:
    settings = _settings(request)
    raw = await request.body()
    verify_signature(
        secret=settings.provider_webhook_secret,
        raw_body=raw,
        signature=request.headers.get("X-Signature"),
        timestamp=request.headers.get("X-Timestamp"),
        tolerance_seconds=settings.webhook_tolerance_seconds,
    )
    event = parse_webhook(raw)
    outcome = await process_webhook(session, event)
    # 200 for duplicates / late events too: they were handled, the provider must not retry.
    return {"received": True, "event_id": event.event_id, "outcome": outcome}


@router.post("/transfers", status_code=201)
async def create_transfer(
    body: TransferIn,
    account: CurrentAccount,
    session: Session,
    idempotency_key: IdempotencyHeader = None,
) -> JSONResponse:
    key = validate_key(idempotency_key, required=True)
    endpoint = "POST /v1/transfers"
    status_code, payload, replayed = await run_idempotent(
        session,
        account_id=account.id,
        key=key,
        endpoint=endpoint,
        request_hash=request_fingerprint(endpoint, body.model_dump(mode="json")),
        operation=lambda: execute_transfer(session, account.id, body.to_account_id, body.amount),
    )
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Idempotent-Replayed": "true" if replayed else "false"},
    )


@router.post("/purchases", status_code=201)
async def create_purchase(
    body: PurchaseIn,
    account: CurrentAccount,
    session: Session,
    idempotency_key: IdempotencyHeader = None,
) -> JSONResponse:
    # Idempotency-Key is optional here, but honoured when present.
    key = validate_key(idempotency_key, required=False)
    endpoint = "POST /v1/purchases"
    status_code, payload, replayed = await run_idempotent(
        session,
        account_id=account.id,
        key=key,
        endpoint=endpoint,
        request_hash=request_fingerprint(endpoint, body.model_dump(mode="json")),
        operation=lambda: execute_purchase(session, account.id, body.credits),
    )
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Idempotent-Replayed": "true" if replayed else "false"},
    )


@router.get("/transactions")
async def get_transactions(
    account: CurrentAccount,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict:
    return await list_transactions(session, account.id, limit=limit, cursor=cursor)
