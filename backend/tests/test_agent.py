"""Diagnosis quality, scoring sanity, and the data boundary around the model."""
from __future__ import annotations

import pytest

from app.agent.diagnosis import diagnose
from app.agent.predictor import score_action, score_actions
from app.agent.prompts import sanitise_for_llm
from app.enums import Action, DiagnosisCategory


class TestDiagnosis:
    def test_bank_timeout_is_technical(self):
        d = diagnose({"failure_reason": "bank_unavailable", "method": "upi"})
        assert d.category is DiagnosisCategory.TEMPORARY_TECHNICAL

    def test_insufficient_funds_is_a_customer_issue(self):
        d = diagnose({"failure_reason": "insufficient_funds", "method": "card"})
        assert d.category is DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE

    def test_abandoned_checkout_is_its_own_category(self):
        d = diagnose({"failure_reason": "checkout_abandoned", "is_checkout_abandonment": True})
        assert d.category is DiagnosisCategory.CHECKOUT_ABANDONMENT

    def test_ambiguous_code_plus_rail_spike_reads_as_infrastructure(self):
        """The inference the whole product turns on.

        `do_not_honour` looks like a customer decline. Concurrent merchant-wide
        failures on the same rail say otherwise.
        """
        quiet = diagnose({
            "failure_reason": "do_not_honour", "method": "upi",
            "recent_failure_spike_ratio": 1.0, "customer_success_rate": 0.2,
            "customer_previous_successful_payments": 0,
        })
        spiking = diagnose({
            "failure_reason": "do_not_honour", "method": "upi",
            "recent_failure_spike_ratio": 4.1, "customer_success_rate": 0.2,
            "customer_previous_successful_payments": 0,
        })

        assert quiet.category is DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE
        assert spiking.category is DiagnosisCategory.TEMPORARY_TECHNICAL
        assert spiking.cause == "rail_degradation"

    def test_a_spike_does_not_excuse_a_dead_card(self):
        """Infrastructure noise must not relabel a genuinely dead instrument."""
        d = diagnose({
            "failure_reason": "expired_card", "method": "card",
            "recent_failure_spike_ratio": 5.0,
        })
        assert d.category is DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE

    def test_exhaustion_overrides_everything(self):
        d = diagnose({
            "failure_reason": "gateway_timeout", "method": "upi",
            "retry_count": 5, "outreach_count": 3, "days_since_failure": 31,
        })
        assert d.category is DiagnosisCategory.LOW_RECOVERY_PROBABILITY


class TestPredictor:
    @pytest.mark.parametrize("reason", ["upi_timeout", "insufficient_funds", "unknown"])
    def test_probabilities_stay_in_range(self, reason):
        features = {"failure_reason": reason, "amount_paise": 500_000, "method": "upi"}
        for scored in score_actions(features, diagnose(features), list(Action)):
            assert 0.0 <= scored.probability <= 1.0

    def test_stacked_evidence_does_not_produce_false_certainty(self):
        """Every positive signal at once must still not read as a sure thing."""
        features = {
            "failure_reason": "upi_timeout", "amount_paise": 500_000, "method": "upi",
            "customer_previous_successful_payments": 40, "customer_success_rate": 1.0,
            "recent_failure_spike_ratio": 6.0, "historical_recovery_rate": 0.95,
            "customer_lifetime_value_paise": 90_000_000,
        }
        scored = score_action(Action.RETRY_DELAYED, features, diagnose(features))
        assert scored.probability <= 0.95

    def test_delay_beats_immediate_retry_during_an_outage(self):
        features = {
            "failure_reason": "bank_unavailable", "amount_paise": 750_000, "method": "upi",
            "recent_failure_spike_ratio": 4.0, "customer_success_rate": 0.9,
            "customer_previous_successful_payments": 8,
        }
        diagnosis = diagnose(features)
        now = score_action(Action.RETRY, features, diagnosis)
        later = score_action(Action.RETRY_DELAYED, features, diagnosis)
        assert later.probability > now.probability

    def test_message_fatigue_is_priced_in(self):
        """Each ignored message makes the next one cost more."""
        base = {"failure_reason": "card_declined", "amount_paise": 500_000, "method": "card"}
        fresh = score_action(Action.SEND_REMINDER, base, diagnose(base))
        tired = score_action(
            Action.SEND_REMINDER, {**base, "outreach_count": 2}, diagnose(base)
        )
        assert tired.action_cost_paise > fresh.action_cost_paise
        assert tired.net_expected_value_paise < fresh.net_expected_value_paise


class TestModelDataBoundary:
    """Design §31: the model is told the minimum needed to reason."""

    def test_personal_data_never_reaches_the_prompt(self):
        payload = sanitise_for_llm({
            "amount_rupees": 4999.0,
            "method": "card",
            "failure_reason": "card_declined",
            # None of the following may cross the boundary:
            "customer_email": "rahul@example.com",
            "customer_phone": "+919812345678",
            "customer_name": "Rahul Sharma",
            "card_number": "4111111111111111",
            "transaction_id": "pay_92831",
            "razorpay_key_secret": "supersecret",
        })

        assert payload == {
            "amount_rupees": 4999.0, "method": "card", "failure_reason": "card_declined",
        }

    def test_allowlist_is_closed_by_default(self):
        """A new column on the model cannot silently start leaking."""
        assert sanitise_for_llm({"some_future_field": "sensitive"}) == {}
