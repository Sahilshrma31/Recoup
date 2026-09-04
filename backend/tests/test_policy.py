"""The guardrails. These are the tests that matter most.

Everything else in this system degrades gracefully; the policy engine is the
component whose failure would let a model spend a merchant's money wrongly.
"""
from __future__ import annotations

import pytest

from app.agent.diagnosis import diagnose
from app.agent.planner import plan
from app.agent.predictor import score_action
from app.enums import Action, PolicyStatus, RecoveryState
from app.policy.guardrails import PolicyEngine
from conftest import features_for


def _evaluate(settings, features, action, **kwargs):
    diagnosis = diagnose(features)
    scored = score_action(action, features, diagnosis)
    return PolicyEngine(settings).evaluate(
        scored, features=features, diagnosis=diagnosis,
        recovery_state=RecoveryState.ANALYZING, **kwargs
    )


def _check(evaluation, name):
    return next(c for c in evaluation.checks if c.name == name)


class TestFutileRetry:
    """Design §13: never re-present an instrument that cannot possibly work."""

    @pytest.mark.parametrize(
        "reason", ["insufficient_funds", "expired_card", "invalid_card", "incorrect_cvv"]
    )
    def test_retry_blocked_for_hard_instrument_failures(
        self, settings, make_transaction, reason
    ):
        txn = make_transaction(failure_reason=reason, method="card")
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.RETRY)

        assert evaluation.blocked
        assert _check(evaluation, "futile_retry").status is PolicyStatus.BLOCKED

    def test_retry_allowed_for_transient_failure(self, settings, make_transaction):
        txn = make_transaction(failure_reason="gateway_timeout")
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.RETRY)

        assert _check(evaluation, "futile_retry").status is PolicyStatus.PASSED

    def test_blocked_retry_falls_through_to_payment_link(self, settings, make_transaction):
        """The signature behaviour: blocked retry -> alternative route, not nothing."""
        txn = make_transaction(failure_reason="insufficient_funds", method="card")
        features = features_for(txn, txn.customer)
        diagnosis = diagnose(features)

        result = plan(
            features, diagnosis, settings=settings,
            recovery_state=RecoveryState.ANALYZING,
            preferred_action=Action.RETRY,      # what the model asked for
            policy_engine=PolicyEngine(settings),
        )

        assert result.action is Action.CREATE_PAYMENT_LINK
        assert result.outcome.overridden_from is Action.RETRY
        assert "insufficient_funds" in result.outcome.override_reason


class TestLimits:
    def test_retry_limit_blocks_further_retries(self, settings, make_transaction):
        txn = make_transaction(retry_count=settings.max_auto_retries)
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.RETRY)

        assert _check(evaluation, "attempt_limit").status is PolicyStatus.BLOCKED

    def test_outreach_limit_blocks_further_messages(self, settings, make_transaction):
        txn = make_transaction(outreach_count=settings.max_outreach_attempts)
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.SEND_REMINDER)

        assert _check(evaluation, "outreach_limit").status is PolicyStatus.BLOCKED

    def test_recovery_window_blocks_stale_transactions(self, settings, make_transaction):
        txn = make_transaction(minutes_ago=60 * 24 * 30)  # 30 days old
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.CREATE_PAYMENT_LINK)

        assert _check(evaluation, "recovery_window").status is PolicyStatus.BLOCKED

    def test_cooldown_blocks_immediate_retry(self, settings, make_transaction):
        txn = make_transaction()
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.RETRY, minutes_since_last_attempt=1.0)

        assert _check(evaluation, "retry_cooldown").status is PolicyStatus.BLOCKED


class TestConsent:
    def test_opted_out_customer_is_never_contacted(
        self, settings, make_customer, make_transaction
    ):
        customer = make_customer("cust_optout", opted_out=True)
        txn = make_transaction(customer=customer)
        features = features_for(txn, customer)

        for action in (Action.SEND_REMINDER, Action.CREATE_PAYMENT_LINK):
            evaluation = _evaluate(settings, features, action)
            assert _check(evaluation, "customer_opt_out").status is PolicyStatus.BLOCKED

    def test_opt_out_does_not_block_a_silent_retry(
        self, settings, make_customer, make_transaction
    ):
        """Opting out of *messages* is not opting out of being charged."""
        customer = make_customer("cust_optout2", opted_out=True)
        txn = make_transaction(customer=customer, failure_reason="gateway_timeout")
        features = features_for(txn, customer)
        evaluation = _evaluate(settings, features, Action.RETRY)

        assert _check(evaluation, "customer_opt_out").status is PolicyStatus.PASSED


class TestApprovalGate:
    def test_large_amount_requires_merchant_approval(self, settings, make_transaction):
        txn = make_transaction(amount_paise=settings.auto_action_limit_paise + 1)
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.CREATE_PAYMENT_LINK)

        assert evaluation.requires_approval
        assert not evaluation.blocked          # allowed, but gated

    def test_amount_at_the_limit_is_still_automatic(self, settings, make_transaction):
        txn = make_transaction(amount_paise=settings.auto_action_limit_paise)
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.CREATE_PAYMENT_LINK)

        assert not evaluation.requires_approval

    def test_merchant_approval_clears_the_gate(self, settings, make_transaction):
        txn = make_transaction(amount_paise=5_000_000)
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.CREATE_PAYMENT_LINK,
                               merchant_approved=True)

        assert not evaluation.requires_approval


class TestStopping:
    def test_exhausted_transaction_stops(self, settings, make_transaction):
        """Design §18: repeated failure + ignored outreach -> deliberate NO_ACTION."""
        txn = make_transaction(
            amount_paise=199_900, failure_reason="card_declined", method="card",
            retry_count=5, outreach_count=3, minutes_ago=60 * 24 * 31,
        )
        features = features_for(txn, txn.customer)
        result = plan(features, diagnose(features), settings=settings,
                      recovery_state=RecoveryState.ANALYZING,
                      policy_engine=PolicyEngine(settings))

        assert result.action is Action.NO_ACTION

    def test_low_value_action_is_not_worth_taking(self, settings, make_transaction):
        """A tiny expected recovery cannot justify spending an action."""
        txn = make_transaction(amount_paise=2_000)  # Rs 20
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.CREATE_PAYMENT_LINK)

        assert _check(evaluation, "expected_value_floor").status is PolicyStatus.BLOCKED


class TestModelCannotBypassPolicy:
    def test_every_rule_runs_on_every_decision(self, settings, make_transaction):
        """The audit trail must record passing checks too, not just the blocker."""
        txn = make_transaction()
        features = features_for(txn, txn.customer)
        evaluation = _evaluate(settings, features, Action.RETRY)

        from app.policy.rules import ALL_RULES

        assert len(evaluation.checks) == len(ALL_RULES)

    def test_model_preference_cannot_skip_a_check(self, settings, make_transaction):
        """A preferred action reorders candidates; it never grants an exemption."""
        txn = make_transaction(
            failure_reason="insufficient_funds", method="card",
            retry_count=settings.max_auto_retries,
        )
        features = features_for(txn, txn.customer)
        result = plan(features, diagnose(features), settings=settings,
                      recovery_state=RecoveryState.ANALYZING,
                      preferred_action=Action.RETRY,
                      policy_engine=PolicyEngine(settings))

        assert result.action is not Action.RETRY

    def test_structurally_impossible_action_is_ignored(self, settings, make_transaction):
        """You cannot re-present a charge for a checkout that never paid."""
        from app.enums import TxnKind

        txn = make_transaction(kind=TxnKind.CHECKOUT, failure_reason="checkout_abandoned")
        features = features_for(txn, txn.customer)
        result = plan(features, diagnose(features), settings=settings,
                      recovery_state=RecoveryState.ANALYZING,
                      preferred_action=Action.RETRY,
                      policy_engine=PolicyEngine(settings))

        assert result.action in (Action.CREATE_PAYMENT_LINK, Action.SEND_REMINDER, Action.NO_ACTION)
