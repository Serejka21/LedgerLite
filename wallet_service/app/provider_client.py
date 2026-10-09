"""HTTP client for the payment provider.

Every failure mode is converted into a ProviderError with a clear category, so the
API layer can answer the client with a stable error code instead of a stack trace.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import Settings
from app.money import format_amount

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """kind: timeout | unavailable | bad_response | rejected"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class InvoiceCreated:
    invoice_id: str
    pay_url: str


@dataclass(frozen=True)
class InvoiceState:
    invoice_id: str
    status: str
    paid_amount: str


class ProviderClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=settings.provider_base_url,
            headers={"Authorization": f"Bearer {settings.provider_api_key}"},
            timeout=httpx.Timeout(
                settings.provider_read_timeout_seconds,
                connect=settings.provider_connect_timeout_seconds,
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            log.warning("provider timeout: %s %s", method, url)
            raise ProviderError("timeout", "Payment provider did not respond in time") from exc
        except httpx.TransportError as exc:
            log.warning("provider unreachable: %s %s: %s", method, url, exc)
            raise ProviderError("unavailable", "Payment provider is unavailable") from exc

        if resp.status_code >= 500:
            raise ProviderError("unavailable", f"Payment provider error ({resp.status_code})")
        if resp.status_code >= 400:
            log.error(
                "provider rejected %s %s: %s %s", method, url, resp.status_code, resp.text[:300]
            )
            raise ProviderError(
                "rejected", f"Payment provider rejected the request ({resp.status_code})"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("bad_response", "Payment provider returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("bad_response", "Payment provider returned unexpected payload")
        return data

    async def create_invoice(
        self, amount: Decimal, external_id: str, callback_url: str
    ) -> InvoiceCreated:
        data = await self._request(
            "POST",
            "/invoices",
            json={
                "amount": format_amount(amount),
                "currency": "USD",
                "external_id": external_id,
                "callback_url": callback_url,
            },
        )
        try:
            return InvoiceCreated(invoice_id=str(data["invoice_id"]), pay_url=str(data["pay_url"]))
        except KeyError as exc:
            raise ProviderError("bad_response", f"Provider response missing {exc}") from exc

    async def get_invoice(self, invoice_id: str) -> InvoiceState:
        data = await self._request("GET", f"/invoices/{invoice_id}")
        try:
            return InvoiceState(
                invoice_id=str(data["invoice_id"]),
                status=str(data["status"]),
                paid_amount=str(data["paid_amount"]),
            )
        except KeyError as exc:
            raise ProviderError("bad_response", f"Provider response missing {exc}") from exc
