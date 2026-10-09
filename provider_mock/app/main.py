"""provider-mock: minimal in-memory imitation of an external payment provider.

Deliberately simple: no persistence, no retries for webhooks, one process.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    provider_api_key: str = "dev-provider-key"
    provider_webhook_secret: str = "dev-webhook-secret"
    provider_public_url: str = "http://localhost:8001"
    webhook_timeout_seconds: float = 5.0


settings = Settings()
app = FastAPI(title="provider-mock")

FINAL_STATUSES = {"paid", "partially_paid", "expired"}
AMOUNT_PATTERN = r"^\d+(\.\d{1,2})?$"


class Invoice(BaseModel):
    invoice_id: str
    amount: str
    currency: str
    external_id: str
    callback_url: str
    status: str = "pending"
    paid_amount: str = "0.00"
    created_at: str


INVOICES: dict[str, Invoice] = {}
_lock = asyncio.Lock()


def require_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = f"Bearer {settings.provider_api_key}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")


class CreateInvoiceIn(BaseModel):
    amount: str = Field(pattern=AMOUNT_PATTERN)
    currency: Literal["USD"]
    external_id: str = Field(min_length=1, max_length=128)
    callback_url: str = Field(min_length=1)

    @field_validator("amount")
    @classmethod
    def positive(cls, v: str) -> str:
        if Decimal(v) <= 0:
            raise ValueError("amount must be positive")
        return v


class SimulateIn(BaseModel):
    status: Literal["paid", "partially_paid", "expired"]
    paid_amount: str | None = Field(default=None, pattern=AMOUNT_PATTERN)
    deliver_webhook: bool = True
    duplicate: bool = False


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = timestamp.encode() + b"." + raw_body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def public_view(inv: Invoice) -> dict:
    return {
        "invoice_id": inv.invoice_id,
        "status": inv.status,
        "amount": inv.amount,
        "currency": inv.currency,
        "paid_amount": inv.paid_amount,
        "external_id": inv.external_id,
        "pay_url": f"{settings.provider_public_url}/pay/{inv.invoice_id}",
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/invoices", status_code=201, dependencies=[Depends(require_api_key)])
async def create_invoice(body: CreateInvoiceIn) -> dict:
    invoice_id = "inv_" + secrets.token_hex(8)
    inv = Invoice(
        invoice_id=invoice_id,
        amount=f"{Decimal(body.amount):.2f}",
        currency=body.currency,
        external_id=body.external_id,
        callback_url=body.callback_url,
        created_at=datetime.now(UTC).isoformat(),
    )
    INVOICES[invoice_id] = inv
    view = public_view(inv)
    return {"invoice_id": invoice_id, "status": inv.status, "pay_url": view["pay_url"]}


@app.get("/invoices/{invoice_id}", dependencies=[Depends(require_api_key)])
async def get_invoice(invoice_id: str) -> dict:
    inv = INVOICES.get(invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    return public_view(inv)


async def deliver_webhook(inv: Invoice, times: int) -> list[dict]:
    payload = {
        "event_id": "evt_" + secrets.token_hex(8),
        "invoice_id": inv.invoice_id,
        "status": inv.status,
        "paid_amount": inv.paid_amount,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
        for _ in range(times):
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "X-Timestamp": ts,
                "X-Signature": sign(settings.provider_webhook_secret, ts, raw),
            }
            try:
                resp = await client.post(inv.callback_url, content=raw, headers=headers)
                results.append({"status_code": resp.status_code, "body": resp.text[:500]})
            except httpx.HTTPError as exc:
                results.append({"error": f"{type(exc).__name__}: {exc}"})
    return [{"event_id": payload["event_id"], **r} for r in results]


@app.post("/invoices/{invoice_id}/simulate", dependencies=[Depends(require_api_key)])
async def simulate(invoice_id: str, body: SimulateIn) -> dict:
    async with _lock:
        inv = INVOICES.get(invoice_id)
        if inv is None:
            raise HTTPException(404, "invoice not found")
        if inv.status in FINAL_STATUSES:
            raise HTTPException(409, f"invoice already final: {inv.status}")

        amount = Decimal(inv.amount)
        if body.status == "expired":
            paid = Decimal("0.00")
        elif body.status == "paid":
            paid = Decimal(body.paid_amount) if body.paid_amount is not None else amount
        else:  # partially_paid
            if body.paid_amount is None:
                raise HTTPException(422, "paid_amount is required for partially_paid")
            paid = Decimal(body.paid_amount)
            if not (Decimal("0") < paid < amount):
                raise HTTPException(422, "partially_paid requires 0 < paid_amount < amount")

        inv.status = body.status
        inv.paid_amount = f"{paid:.2f}"

    deliveries: list[dict] = []
    if body.deliver_webhook:
        deliveries = await deliver_webhook(inv, times=2 if body.duplicate else 1)
    return {"invoice": public_view(inv), "webhook_deliveries": deliveries}
