"""Gateway contract shared by the live and mock adapters.

Keeping this narrow is deliberate. The agent can only ever reach the payment
network through these few methods, so the blast radius of a bad decision is
bounded by this file, not by the Razorpay API surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class GatewayError(RuntimeError):
    """Provider call failed. `retryable` drives the backoff policy in §30."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class GatewayResult:
    ok: bool
    reference: str | None = None       # provider id (plink_..., order_...)
    url: str | None = None             # customer-facing URL, when there is one
    status: str | None = None
    raw: dict = field(default_factory=dict)


class PaymentGateway(Protocol):
    """Minimal surface the recovery agent is allowed to touch."""

    name: str

    async def fetch_payment(self, payment_id: str) -> dict: ...

    async def fetch_order(self, order_id: str) -> dict: ...

    async def create_order(self, *, amount_paise: int, currency: str, notes: dict) -> GatewayResult: ...

    async def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str,
        description: str,
        customer: dict,
        notes: dict,
        notify: bool,
        reference_id: str,
    ) -> GatewayResult: ...

    async def send_reminder(self, *, payment_link_id: str) -> GatewayResult: ...
