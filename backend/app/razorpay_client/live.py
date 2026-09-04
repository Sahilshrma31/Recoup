"""Live Razorpay adapter (test-mode keys are enough to exercise all of it).

A note on what "RETRY" can honestly mean here. Razorpay has no server-initiated
re-charge for a *failed one-off payment*: the customer authorises each attempt.
So a retry is implemented as re-presenting the same order to the customer
through a fresh, short-lived link tagged as a retry -- the same order, the same
amount, no duplicate charge risk -- rather than pretending the server can debit
the account on its own. Recurring mandates are the exception, and that is why
RETRY_SUBSCRIPTION is a separate action.
"""
from __future__ import annotations

import asyncio
import logging

import razorpay

from ..config import Settings
from .base import GatewayError, GatewayResult, PaymentGateway

log = logging.getLogger(__name__)


class LiveRazorpayGateway(PaymentGateway):
    name = "razorpay"

    def __init__(self, settings: Settings) -> None:
        if not settings.razorpay_configured:
            raise ValueError("Razorpay key id/secret are required for the live gateway.")
        self._client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        self._client.set_app_details({"title": "revenue-recovery-agent", "version": "1.0.0"})

    async def _call(self, fn, *args, **kwargs):
        """Run the blocking SDK off the event loop and normalise its errors."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except razorpay.errors.BadRequestError as exc:
            # A rejected request will be rejected identically on every retry.
            raise GatewayError(f"Razorpay rejected the request: {exc}", retryable=False) from exc
        except razorpay.errors.GatewayError as exc:
            raise GatewayError(f"Razorpay gateway error: {exc}", retryable=True) from exc
        except razorpay.errors.ServerError as exc:
            raise GatewayError(f"Razorpay server error: {exc}", retryable=True) from exc
        except Exception as exc:  # network layer, DNS, TLS
            raise GatewayError(f"Razorpay call failed: {exc}", retryable=True) from exc

    async def fetch_payment(self, payment_id: str) -> dict:
        return await self._call(self._client.payment.fetch, payment_id)

    async def fetch_order(self, order_id: str) -> dict:
        return await self._call(self._client.order.fetch, order_id)

    async def create_order(self, *, amount_paise: int, currency: str, notes: dict) -> GatewayResult:
        data = await self._call(
            self._client.order.create,
            {"amount": amount_paise, "currency": currency, "notes": notes, "payment_capture": 1},
        )
        return GatewayResult(ok=True, reference=data.get("id"), status=data.get("status"), raw=data)

    async def create_payment_link(
        self, *, amount_paise: int, currency: str, description: str, customer: dict,
        notes: dict, notify: bool, reference_id: str,
    ) -> GatewayResult:
        payload = {
            "amount": amount_paise,
            "currency": currency,
            "description": description[:2048],
            "customer": {
                k: v for k, v in
                {"name": customer.get("name"), "email": customer.get("email"), "contact": customer.get("phone")}.items()
                if v
            },
            # `reference_id` is unique per link at Razorpay, so passing the
            # agent's idempotency key makes a duplicate webhook a no-op at the
            # provider as well as in our own database.
            "reference_id": reference_id,
            "notify": {"sms": bool(notify), "email": bool(notify)},
            "reminder_enable": bool(notify),
            "notes": notes,
        }
        data = await self._call(self._client.payment_link.create, payload)
        return GatewayResult(
            ok=True, reference=data.get("id"), url=data.get("short_url"),
            status=data.get("status"), raw=data,
        )

    async def send_reminder(self, *, payment_link_id: str) -> GatewayResult:
        data = await self._call(self._client.payment_link.notify_by, payment_link_id, "sms")
        return GatewayResult(ok=True, reference=payment_link_id, status="reminder_sent", raw=data or {})
