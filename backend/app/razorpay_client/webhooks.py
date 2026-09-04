"""Webhook signature verification (design §31).

An unverified webhook is an unauthenticated instruction to spend money, so a
configured secret is enforced strictly: a bad signature is rejected, never
logged-and-accepted.
"""
from __future__ import annotations

import hashlib
import hmac

from ..config import Settings


class InvalidSignature(ValueError):
    pass


def verify_signature(body: bytes, signature: str | None, settings: Settings) -> bool:
    """Return True if the payload is authentic.

    With no secret configured (local demo) verification is skipped, and the API
    layer surfaces that in its health output so it can never be mistaken for a
    verified deployment.
    """
    secret = settings.razorpay_webhook_secret
    if not secret:
        return True
    if not signature:
        raise InvalidSignature("Missing X-Razorpay-Signature header.")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature("Webhook signature does not match.")
    return True
