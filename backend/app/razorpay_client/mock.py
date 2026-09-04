"""In-memory gateway used whenever Razorpay credentials are absent.

It exists so the entire agent -- webhooks, state machine, idempotency, policy,
verification -- can be run and demonstrated end to end with no live account,
and so the A/B experiment can execute tens of thousands of actions offline.
It never contacts a network.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import time

from .base import GatewayError, GatewayResult, PaymentGateway


def _rid(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class MockGateway(PaymentGateway):
    name = "mock"

    def __init__(self, *, latency_seconds: float = 0.05, failure_rate: float = 0.0, seed: int | None = None) -> None:
        self._latency = latency_seconds
        self._failure_rate = failure_rate  # exercises the §30 backoff path on demand
        self._rng = random.Random(seed)
        self._orders: dict[str, dict] = {}
        self._links: dict[str, dict] = {}
        self._payments: dict[str, dict] = {}

    async def _simulate_call(self) -> None:
        if self._latency:
            await asyncio.sleep(self._latency)
        if self._failure_rate and self._rng.random() < self._failure_rate:
            raise GatewayError("Simulated provider outage", retryable=True)

    async def fetch_payment(self, payment_id: str) -> dict:
        await self._simulate_call()
        return self._payments.get(payment_id, {"id": payment_id, "status": "failed"})

    async def fetch_order(self, order_id: str) -> dict:
        await self._simulate_call()
        return self._orders.get(order_id, {"id": order_id, "status": "attempted", "amount_paid": 0})

    async def create_order(self, *, amount_paise: int, currency: str, notes: dict) -> GatewayResult:
        await self._simulate_call()
        oid = _rid("order")
        self._orders[oid] = {
            "id": oid, "amount": amount_paise, "currency": currency,
            "status": "created", "notes": notes, "created_at": int(time.time()),
        }
        return GatewayResult(ok=True, reference=oid, status="created", raw=self._orders[oid])

    async def create_payment_link(
        self, *, amount_paise: int, currency: str, description: str, customer: dict,
        notes: dict, notify: bool, reference_id: str,
    ) -> GatewayResult:
        await self._simulate_call()
        lid = _rid("plink")
        url = f"https://rzp.io/i/{secrets.token_urlsafe(8)}"
        self._links[lid] = {
            "id": lid, "short_url": url, "amount": amount_paise, "currency": currency,
            "description": description, "reference_id": reference_id, "status": "created",
            "notes": notes, "notify": notify,
        }
        return GatewayResult(ok=True, reference=lid, url=url, status="created", raw=self._links[lid])

    async def send_reminder(self, *, payment_link_id: str) -> GatewayResult:
        await self._simulate_call()
        return GatewayResult(ok=True, reference=payment_link_id, status="reminder_sent")

    # --- test/demo helpers (not part of the gateway contract) ---

    def mark_paid(self, reference: str, amount_paise: int) -> None:
        if reference in self._links:
            self._links[reference]["status"] = "paid"
        if reference in self._orders:
            self._orders[reference].update({"status": "paid", "amount_paid": amount_paise})
