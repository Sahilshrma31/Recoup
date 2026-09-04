"""Gateway selection and the §30 retry-with-backoff wrapper."""
from __future__ import annotations

import asyncio
import logging
import random

from ..config import Settings
from .base import GatewayError, GatewayResult, PaymentGateway
from .mock import MockGateway

log = logging.getLogger(__name__)

_MAX_PROVIDER_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 0.4


def build_gateway(settings: Settings) -> PaymentGateway:
    if settings.live_execution:
        from .live import LiveRazorpayGateway

        log.info("Using live Razorpay gateway")
        return LiveRazorpayGateway(settings)
    log.info("Using mock gateway (no Razorpay credentials, or RAZORPAY_LIVE=false)")
    return MockGateway()


async def call_with_backoff(coro_factory, *, max_attempts: int = _MAX_PROVIDER_ATTEMPTS) -> tuple[GatewayResult, int]:
    """Retry a gateway call with exponential backoff and jitter.

    Bounded on purpose: when the provider is still failing after the last
    attempt the caller marks the recovery attempt `pending_manual` and tells
    the merchant, rather than retrying a payment API indefinitely.
    """
    last: GatewayError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory(), attempt
        except GatewayError as exc:
            last = exc
            if not exc.retryable or attempt == max_attempts:
                break
            delay = _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)  # jitter, so retries don't sync up
            log.warning("Gateway call failed (attempt %d/%d): %s -- retrying in %.2fs",
                        attempt, max_attempts, exc, delay)
            await asyncio.sleep(delay)
    raise last or GatewayError("Gateway call failed")
