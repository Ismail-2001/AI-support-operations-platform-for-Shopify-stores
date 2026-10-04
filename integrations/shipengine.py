"""ShipEngine shipping API client — return labels only (https://apidocs.shipengine.com).

Auth: `API-Key: <token>` header. Base: https://api.shipengine.com/v1

Chosen over EasyPost because ShipEngine has a native return-label flow
(`is_return_label: true` on the label purchase) instead of a separate return
API surface. Decision recorded in docs/integrations.md.

Flow for a prepaid return label:
  1. POST /v1/rates        — get carrier rates for the RETURN shipment
     (ship_from = the customer's address from the order, ship_to = RETURN_ADDRESS_*)
  2. POST /v1/labels       — buy the cheapest rate with is_return_label: true,
     which is what makes it a return label (and what the RMA number rides on).

The actual cost comes back on the label response (`rate.amount`) — never the
estimate, so the audit trail records what was really charged.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from agent.config import settings
from api.errors import APIError, raise_not_configured

logger = structlog.get_logger(__name__)

_EXP_BACKOFF = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=8),
}


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600
    )


def _log_retry(retry_state) -> None:
    logger.warning(
        "shipengine_retry",
        function=getattr(retry_state.fn, "__name__", str(retry_state.fn)),
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()),
    )


def _address_block(prefix: str, values: dict[str, str | None]) -> dict[str, Any]:
    block: dict[str, Any] = {
        "name": values.get("name") or "Customer",
        "address1": values.get("address1") or "",
        "city": values.get("city") or "",
        "state": values.get("state") or "",
        "postal_code": values.get("zip") or "",
        "country": values.get("country") or "US",
        "address_residential_indicator": "residential" if prefix == "from" else "commercial",
    }
    if values.get("address2"):
        block["address2"] = values["address2"]
    if values.get("phone"):
        block["phone"] = str(values["phone"])
    return block


class ShipEngineClient:
    BASE_URL = "https://api.shipengine.com/v1"

    @property
    def enabled(self) -> bool:
        return bool(settings.SHIPENGINE_API_KEY)

    @property
    def configured(self) -> tuple[bool, list[str]]:
        """(enabled, list of missing RETURN_ADDRESS_* fields) — the return-eligibility
        endpoint surfaces these so the agent can tell the operator what to fill in."""
        missing = [
            f
            for f in ("RETURN_ADDRESS_NAME", "RETURN_ADDRESS1", "RETURN_CITY", "RETURN_ZIP")
            if not getattr(settings, f)
        ]
        return (self.enabled and not missing, missing)

    def _headers(self) -> dict[str, str]:
        if not self.enabled:
            raise_not_configured("ShipEngine")
        return {"API-Key": settings.SHIPENGINE_API_KEY.get_secret_value()}

    @retry(
        retry=retry_if_exception(_is_transient),
        before_sleep=_log_retry,
        **_EXP_BACKOFF,
    )
    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self.BASE_URL}{path}", headers=self._headers(), json=body)
        if resp.status_code >= 400:
            # ShipEngine error bodies carry a `message` (and often a details array).
            try:
                payload = resp.json()
                message = payload.get("message") or payload.get("errors")
                if isinstance(message, list):
                    message = "; ".join(str(m) for m in message)
            except Exception:
                message = resp.text[:300]
            raise APIError(
                code="SHIPENGINE_ERROR",
                message=f"ShipEngine {path} failed ({resp.status_code}): {message}",
                status=502,
            )
        return resp.json()

    def _package(self) -> dict[str, Any]:
        return {
            "weight": {
                "value": float(settings.RETURN_PACKAGE_WEIGHT_OZ),
                "unit": "ounce",
            },
            "dimensions": {
                "unit": "inch",
                "length": float(settings.RETURN_PACKAGE_LENGTH_IN),
                "width": float(settings.RETURN_PACKAGE_WIDTH_IN),
                "height": float(settings.RETURN_PACKAGE_HEIGHT_IN),
            },
        }

    def _return_shipment(self, customer_address: dict[str, str]) -> dict[str, Any]:
        """Return shipment: FROM the customer TO the store's return address."""
        return {
            "validate_address": "no_validation",
            "ship_from": _address_block(
                "from",
                {
                    "name": customer_address.get("name"),
                    "address1": customer_address.get("address1"),
                    "address2": customer_address.get("address2"),
                    "city": customer_address.get("city"),
                    "state": customer_address.get("state"),
                    "zip": customer_address.get("zip"),
                    "country": customer_address.get("country"),
                    "phone": customer_address.get("phone"),
                },
            ),
            "ship_to": _address_block(
                "to",
                {
                    "name": settings.RETURN_ADDRESS_NAME,
                    "address1": settings.RETURN_ADDRESS1,
                    "address2": settings.RETURN_ADDRESS2,
                    "city": settings.RETURN_CITY,
                    "state": settings.RETURN_STATE,
                    "zip": settings.RETURN_ZIP,
                    "country": settings.RETURN_COUNTRY,
                    "phone": settings.RETURN_PHONE,
                },
            ),
            "packages": [self._package()],
        }

    async def get_rates(self, customer_address: dict[str, str]) -> list[dict[str, Any]]:
        """All available carrier rates for the return shipment, cheapest first."""
        body = {
            "rate_options": {"carrier_ids": []},
            "shipment": self._return_shipment(customer_address),
        }
        data = await self._post("/rates", body)
        rates = (data.get("rate_response") or {}).get("rates") or []
        return sorted(rates, key=lambda r: float(((r.get("ship_rate") or {}).get("amount")) or 0))

    async def buy_return_label(
        self,
        customer_address: dict[str, str],
        rate_id: str,
        rma_number: str | None = None,
    ) -> dict[str, Any]:
        """Purchase a prepaid return label for the given rate."""
        body: dict[str, Any] = {
            "rate_id": rate_id,
            "label_format": "pdf",
            "display_scheme": "4x6",
            "label_layout": "4x6",
            "is_return_label": True,
        }
        if rma_number:
            body["rma_number"] = str(rma_number)
        data = await self._post("/labels", body)
        label = {
            "label_id": data.get("label_id"),
            "label_url": (data.get("label_download") or {}).get("url"),
            "tracking_number": data.get("tracking_number"),
            "tracking_url": data.get("tracking_url"),
            "status": data.get("status"),
            "cost_usd": float((data.get("rate") or {}).get("amount") or 0),
            "currency": (data.get("rate") or {}).get("currency") or "USD",
            "carrier": (data.get("rate") or {}).get("carrier_friendly_name"),
            "service_code": (data.get("rate") or {}).get("service_code"),
            "rma_number": rma_number,
        }
        logger.info(
            "return_label_purchased",
            label_id=label["label_id"],
            tracking_number=label["tracking_number"],
            cost_usd=label["cost_usd"],
        )
        return label

    async def quote_and_buy(
        self,
        customer_address: dict[str, str],
        rma_number: str | None = None,
        rate_id: str | None = None,
    ) -> dict[str, Any]:
        """One-call helper: fetch rates (unless a rate was pre-approved), pick the
        cheapest, buy the label. Returns the label dict above."""
        if rate_id is None:
            rates = await self.get_rates(customer_address)
            if not rates:
                raise APIError(
                    code="NO_RATES_AVAILABLE",
                    message="No carrier rates available for this return shipment",
                    status=422,
                )
            rate_id = rates[0].get("rate_id")
        return await self.buy_return_label(customer_address, rate_id, rma_number=rma_number)
