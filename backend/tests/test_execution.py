"""Execution safety: idempotency, state transitions, and provider failure."""
from __future__ import annotations

import pytest

from app.enums import Action, AttemptStatus, RecoveryState
from app.models import AgentDecision
from app.razorpay_client.factory import call_with_backoff
from app.razorpay_client.mock import MockGateway
from app.services.executor import ActionExecutor, idempotency_key
from app.services.state_machine import InvalidTransition, transition


def _decision(txn, action=Action.CREATE_PAYMENT_LINK, delay=0):
    return AgentDecision(
        transaction_id=txn.id, diagnosis="test_cause", category="B_CUSTOMER_PAYMENT_ISSUE",
        diagnosis_confidence=0.9, recommended_action=str(action), action=str(action),
        delay_minutes=delay, recovery_probability=0.7, expected_recovery_paise=100_000,
        reasoning_summary="test", source="rules", features={}, action_scores=[], policy_result={},
    )


class TestIdempotency:
    """Design §29: a duplicate webhook must never produce a second charge."""

    @pytest.mark.asyncio
    async def test_duplicate_execution_creates_one_attempt(
        self, db, settings, make_transaction
    ):
        txn = make_transaction()
        decision = _decision(txn)
        db.add(decision)
        db.flush()

        executor = ActionExecutor(settings, MockGateway(latency_seconds=0))

        first = await executor.execute(db, txn, decision)
        # Replaying the same event, exactly as a webhook retry would.
        second = await executor.execute(db, txn, decision)

        assert first.id == second.id
        assert db.query(type(first)).count() == 1

    def test_key_is_derived_from_transaction_and_attempt(self):
        assert idempotency_key("pay_92831", 1) == "recovery_pay_92831_1"
        assert idempotency_key("pay_92831", 2) != idempotency_key("pay_92831", 1)

    @pytest.mark.asyncio
    async def test_counters_move_only_after_an_action_goes_out(
        self, db, settings, make_transaction
    ):
        txn = make_transaction()
        assert txn.outreach_count == 0

        decision = _decision(txn, Action.SEND_REMINDER)
        db.add(decision)
        db.flush()

        executor = ActionExecutor(settings, MockGateway(latency_seconds=0))
        await executor.execute(db, txn, decision)

        assert txn.outreach_count == 1


class TestProviderFailure:
    """Design §30: bounded retries, then hand it to a human -- never hammer."""

    @pytest.mark.asyncio
    async def test_exhausted_provider_marks_attempt_for_the_merchant(
        self, db, settings, make_transaction
    ):
        txn = make_transaction()
        decision = _decision(txn)
        db.add(decision)
        db.flush()

        dead_gateway = MockGateway(latency_seconds=0, failure_rate=1.0)
        executor = ActionExecutor(settings, dead_gateway)
        attempt = await executor.execute(db, txn, decision)

        assert attempt.status == AttemptStatus.PENDING_MANUAL
        assert attempt.error
        assert txn.recovery_state == RecoveryState.ATTEMPT_FAILED

    @pytest.mark.asyncio
    async def test_backoff_gives_up_rather_than_retrying_forever(self):
        gateway = MockGateway(latency_seconds=0, failure_rate=1.0)
        calls = {"n": 0}

        async def failing():
            calls["n"] += 1
            return await gateway.create_order(amount_paise=1000, currency="INR", notes={})

        from app.razorpay_client.base import GatewayError

        with pytest.raises(GatewayError):
            await call_with_backoff(failing, max_attempts=3)
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_is_not_retried(self):
        from app.razorpay_client.base import GatewayError

        calls = {"n": 0}

        async def rejected():
            calls["n"] += 1
            raise GatewayError("bad request", retryable=False)

        with pytest.raises(GatewayError):
            await call_with_backoff(rejected, max_attempts=3)
        assert calls["n"] == 1, "a rejected request will be rejected identically every time"


class TestStateMachine:
    """Design §15: the workflow, not a status string somebody wrote."""

    def test_legal_path_to_recovered(self, make_transaction):
        txn = make_transaction()
        for state in (RecoveryState.ANALYZING, RecoveryState.PLANNED,
                      RecoveryState.EXECUTING, RecoveryState.RECOVERED):
            transition(txn, state)
        assert txn.recovery_state == RecoveryState.RECOVERED
        assert txn.at_risk is False

    def test_recovered_is_terminal(self, make_transaction):
        txn = make_transaction()
        for state in (RecoveryState.ANALYZING, RecoveryState.PLANNED,
                      RecoveryState.EXECUTING, RecoveryState.RECOVERED):
            transition(txn, state)

        with pytest.raises(InvalidTransition):
            transition(txn, RecoveryState.EXECUTING)

    def test_cannot_skip_from_detected_to_executing(self, make_transaction):
        txn = make_transaction()
        with pytest.raises(InvalidTransition):
            transition(txn, RecoveryState.EXECUTING)

    def test_stopping_records_the_reason_and_clears_risk(self, make_transaction):
        txn = make_transaction()
        transition(txn, RecoveryState.ANALYZING)
        transition(txn, RecoveryState.STOPPED, reason="recovery_exhausted")

        assert txn.stop_reason == "recovery_exhausted"
        assert txn.at_risk is False

    @pytest.mark.asyncio
    async def test_no_action_stops_without_touching_the_gateway(
        self, db, settings, make_transaction
    ):
        txn = make_transaction()
        transition(txn, RecoveryState.ANALYZING)
        decision = _decision(txn, Action.NO_ACTION)
        db.add(decision)
        db.flush()

        gateway = MockGateway(latency_seconds=0, failure_rate=1.0)  # would raise if called
        attempt = await ActionExecutor(settings, gateway).execute(db, txn, decision)

        assert attempt is None
        assert txn.recovery_state == RecoveryState.STOPPED
