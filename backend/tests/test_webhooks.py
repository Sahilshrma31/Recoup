"""Webhook authenticity. An unverified webhook is an instruction to spend money."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.config import Settings
from app.razorpay_client.webhooks import InvalidSignature, verify_signature

SECRET = "whsec_test_123"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def secured() -> Settings:
    return Settings(database_url="sqlite://", razorpay_webhook_secret=SECRET)


@pytest.fixture
def open_demo() -> Settings:
    return Settings(database_url="sqlite://", razorpay_webhook_secret=None)


def _body(**extra) -> bytes:
    return json.dumps({"event": "payment.failed", **extra}).encode()


class TestVerification:
    def test_valid_signature_passes(self, secured):
        body = _body()
        assert verify_signature(body, _sign(body), secured) is True

    def test_tampered_body_is_rejected(self, secured):
        signature = _sign(_body())
        with pytest.raises(InvalidSignature):
            verify_signature(_body(amount=999999), signature, secured)

    def test_wrong_secret_is_rejected(self, secured):
        body = _body()
        with pytest.raises(InvalidSignature):
            verify_signature(body, _sign(body, "whsec_attacker"), secured)

    def test_missing_signature_header_is_rejected(self, secured):
        with pytest.raises(InvalidSignature):
            verify_signature(_body(), None, secured)

    def test_non_utf8_body_is_rejected(self, secured):
        with pytest.raises(InvalidSignature):
            verify_signature(b"\xff\xfe\x00bad", "deadbeef", secured)

    def test_unicode_body_round_trips(self, secured):
        """Rupee signs and Indian names must not break the byte/str boundary."""
        body = json.dumps({"event": "payment.failed", "note": "₹4,999 — Ananya"}).encode()
        assert verify_signature(body, _sign(body), secured) is True


class TestDemoMode:
    def test_verification_is_skipped_without_a_secret(self, open_demo):
        """Deliberate, so the demo runs offline -- and reported by /health."""
        assert verify_signature(_body(), None, open_demo) is True
