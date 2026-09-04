"""Webhook signature verification (design §31).

An unverified webhook is an unauthenticated instruction to spend money, so a
configured secret is enforced strictly: a bad signature is rejected, never
logged-and-accepted.

Verification is delegated to the Razorpay SDK's own `verify_webhook_signature`
rather than reimplemented here. The HMAC is easy to write and easy to get
subtly wrong, and if Razorpay ever changes the scheme the SDK is what gets
updated. We adapt its interface in two small ways: it wants `str` and signals
failure by raising, whereas the call site here has `bytes` and wants a bool.
"""
from __future__ import annotations

import razorpay
from razorpay.errors import SignatureVerificationError

from ..config import Settings


class InvalidSignature(ValueError):
    pass


#: The SDK's utility needs no credentials for signature checks, but it hangs
#: off a client, so one is built once at import.
_utility = razorpay.Client(auth=("", "")).utility


def verify_signature(body: bytes, signature: str | None, settings: Settings) -> bool:
    """Return True if the payload is authentic, else raise `InvalidSignature`.

    With no secret configured (local demo) verification is skipped, and the API
    layer surfaces that in its health output so it can never be mistaken for a
    verified deployment.
    """
    secret = settings.razorpay_webhook_secret
    if not secret:
        return True
    if not signature:
        raise InvalidSignature("Missing X-Razorpay-Signature header.")

    try:
        # The SDK re-encodes internally, so hand it text, not bytes. Decoding
        # strictly is deliberate: a body that is not valid UTF-8 did not come
        # from Razorpay, and guessing at an encoding here would be a way to
        # accidentally validate a payload we cannot faithfully reproduce.
        payload = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSignature("Webhook body is not valid UTF-8.") from exc

    try:
        _utility.verify_webhook_signature(payload, signature, secret)
    except SignatureVerificationError as exc:
        raise InvalidSignature("Webhook signature does not match.") from exc
    return True
