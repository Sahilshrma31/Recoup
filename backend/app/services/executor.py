"""Action execution (design §14, §29, §30).

Everything that touches money passes through `execute`. Three properties are
non-negotiable here:

* **Idempotent.** Every financial action carries a key derived from the
  transaction and attempt number, and is stored under a unique constraint. A
  duplicate webhook cannot produce a second charge attempt.
* **Bounded.** Provider calls retry with backoff a fixed number of times, then
  stop and ask a human, rather than hammering a payment API.
* **Recorded.** The attempt row is written before the call and updated after,
  so a crash mid-flight leaves evidence rather than a silent gap.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..enums import (
    FINANCIAL_ACTIONS,
    OUTREACH_ACTIONS,
    RETRY_ACTIONS,
    Action,
    AttemptStatus,
    RecoveryState,
)
from ..models import AgentDecision, RecoveryAttempt, Transaction, utcnow
from ..razorpay_client.base import GatewayError, PaymentGateway
from ..razorpay_client.factory import call_with_backoff
from . import activity, simulator
from .state_machine import advance_to, transition

log = logging.getLogger(__name__)


def idempotency_key(txn_id: str, attempt_number: int) -> str:
    """`recovery_<payment_id>_<attempt_number>` -- design §29."""
    return f"recovery_{txn_id}_{attempt_number}"


def _existing_attempt(db: Session, key: str) -> RecoveryAttempt | None:
    return db.execute(
        select(RecoveryAttempt).where(RecoveryAttempt.idempotency_key == key)
    ).scalar_one_or_none()


def _attempt_for_decision(db: Session, decision_id: int | None) -> RecoveryAttempt | None:
    """The attempt already created for this decision, if any."""
    if decision_id is None:
        return None
    return db.execute(
        select(RecoveryAttempt).where(RecoveryAttempt.decision_id == decision_id).limit(1)
    ).scalar_one_or_none()


def next_attempt_number(txn: Transaction) -> int:
    """Ordinal of the next attempt on this transaction.

    Counted from the attempts already recorded, **not** from `retry_count` /
    `outreach_count`. Those counters are incremented by the action itself, so
    deriving the key from them would give a replayed event a fresh key -- and a
    fresh charge. The number of existing attempts does not move until an
    attempt row is actually written.
    """
    return len(txn.attempts) + 1


class ActionExecutor:
    def __init__(self, settings: Settings, gateway: PaymentGateway) -> None:
        self.settings = settings
        self.gateway = gateway
        self._rng = random.Random()

    # -- helpers ----------------------------------------------------------

    def _resolution_delay(self, action: Action) -> timedelta:
        """Compressed wall-clock: one simulated minute == `simulated_minute_seconds`."""
        minutes = simulator.resolution_minutes(action, self._rng)
        return timedelta(seconds=minutes * self.settings.simulated_minute_seconds)

    def _schedule_delay(self, minutes: int) -> timedelta:
        return timedelta(seconds=minutes * self.settings.simulated_minute_seconds)

    # -- main entry point --------------------------------------------------

    async def execute(
        self,
        db: Session,
        txn: Transaction,
        decision: AgentDecision,
        *,
        force_now: bool = False,
    ) -> RecoveryAttempt | None:
        """Carry out the decided action. Returns None for NO_ACTION."""
        action = Action(decision.action)

        if action is Action.NO_ACTION:
            transition(txn, RecoveryState.STOPPED, reason=decision.diagnosis)
            activity.emit(
                db, transaction_id=txn.id, stage="act", level="stop",
                message="Stopped: further recovery attempts are not worth the expected value.",
                detail={"reason": decision.reasoning_summary},
            )
            return None

        # Idempotency gate -- two layers, because there are two ways to double up.
        #
        # 1. The same *decision* executed twice (an event replay, a retried
        #    request, a worker that crashed after acting but before committing).
        #    One decision authorises exactly one action, so this is decisive.
        existing = _attempt_for_decision(db, decision.id)
        if existing is not None:
            activity.emit(
                db, transaction_id=txn.id, stage="act", level="warn",
                message=f"Duplicate suppressed: decision {decision.id} has already been executed.",
                detail={"attempt_id": existing.id, "status": existing.status},
            )
            return existing

        attempt_no = next_attempt_number(txn)
        key = idempotency_key(txn.id, attempt_no)

        # 2. The same attempt ordinal reached by another path. The key is also
        #    sent to Razorpay as the payment link's `reference_id`, so the
        #    provider rejects a duplicate even if this process races itself.
        if (existing := _existing_attempt(db, key)) is not None:
            activity.emit(
                db, transaction_id=txn.id, stage="act", level="warn",
                message=f"Duplicate suppressed: {key} has already been executed.",
                detail={"attempt_id": existing.id, "status": existing.status},
            )
            return existing

        attempt = RecoveryAttempt(
            transaction_id=txn.id,
            decision_id=decision.id,
            action=str(action),
            reason=decision.reasoning_summary,
            confidence=decision.diagnosis_confidence,
            recovery_probability=decision.recovery_probability,
            expected_recovery_paise=decision.expected_recovery_paise,
            attempt_number=attempt_no,
            idempotency_key=key,
            status=AttemptStatus.SCHEDULED,
        )

        # A delayed retry is scheduled, not executed: the whole point is to let
        # the failing rail recover before touching it again.
        if action is Action.RETRY_DELAYED and decision.delay_minutes > 0 and not force_now:
            attempt.scheduled_for = utcnow() + self._schedule_delay(decision.delay_minutes)
            db.add(attempt)
            transition(txn, RecoveryState.PLANNED)
            db.flush()
            activity.emit(
                db, transaction_id=txn.id, stage="act",
                message=f"Scheduled a re-attempt in {decision.delay_minutes} minutes.",
                detail={"attempt_id": attempt.id, "idempotency_key": key},
            )
            return attempt

        db.add(attempt)
        db.flush()
        return await self._perform(db, txn, attempt)

    async def _perform(self, db: Session, txn: Transaction, attempt: RecoveryAttempt) -> RecoveryAttempt:
        """Run the provider call for an attempt that is due now."""
        action = Action(attempt.action)
        attempt.status = AttemptStatus.EXECUTING
        attempt.executed_at = utcnow()
        # Walk the state machine rather than jumping: an attempt executed
        # directly (a scheduled retry firing, or a replayed event) may still be
        # sitting in DETECTED, and skipping states would break the audit trail.
        advance_to(txn, RecoveryState.EXECUTING)

        # Commit the "about to call the provider" state before calling it.
        #
        # Two reasons, and the second is the important one. It releases the
        # SQLite write lock so a slow gateway (or its backoff chain) cannot
        # starve the worker. And it makes the attempt durable *before* money
        # can move: if this process dies mid-call, the attempt row already
        # exists with its idempotency key, so recovery sees an in-flight
        # attempt instead of silently losing that it ever happened.
        db.commit()

        try:
            result, provider_attempts = await call_with_backoff(
                lambda: self._gateway_call(txn, attempt)
            )
        except GatewayError as exc:
            # Bounded failure: stop calling, keep the attempt, tell the merchant.
            attempt.status = AttemptStatus.PENDING_MANUAL
            attempt.error = str(exc)
            attempt.completed_at = utcnow()
            transition(txn, RecoveryState.ATTEMPT_FAILED)
            activity.emit(
                db, transaction_id=txn.id, stage="act", level="error",
                message=f"Payment provider unreachable -- {action} left pending for the merchant.",
                detail={"error": str(exc), "attempt_id": attempt.id},
            )
            return attempt

        attempt.provider_attempts = provider_attempts
        attempt.provider_ref = result.reference
        attempt.provider_url = result.url
        attempt.status = AttemptStatus.AWAITING_CUSTOMER
        attempt.resolve_at = utcnow() + self._resolution_delay(action)

        # Counters move only once an action has actually gone out, so a blocked
        # or failed action never consumes the customer's limited patience.
        if action in RETRY_ACTIONS:
            txn.retry_count += 1
        if action in OUTREACH_ACTIONS:
            txn.outreach_count += 1
            txn.customer.reminders_sent += 1

        activity.emit(
            db, transaction_id=txn.id, stage="act",
            message=self._describe(action, result.url),
            detail={
                "attempt_id": attempt.id,
                "idempotency_key": attempt.idempotency_key,
                "provider": self.gateway.name,
                "provider_ref": result.reference,
                "url": result.url,
            },
        )
        return attempt

    async def _gateway_call(self, txn: Transaction, attempt: RecoveryAttempt):
        action = Action(attempt.action)
        customer = txn.customer
        notes = {
            "recovery_agent": "true",
            "original_transaction": txn.id,
            "action": str(action),
            "attempt": str(attempt.attempt_number),
        }

        if action in FINANCIAL_ACTIONS:
            # Retries and alternative routes both resolve to a payment link; the
            # difference is that a retry re-presents the *same order* silently,
            # while an alternative route is a new, notified request. See the
            # note in razorpay_client/live.py on why a server-side re-charge of
            # a failed one-off payment is not a thing.
            notify = action is Action.CREATE_PAYMENT_LINK
            description = (
                f"Payment retry for order {txn.razorpay_order_id or txn.id}"
                if action in RETRY_ACTIONS
                else f"Complete your payment for order {txn.razorpay_order_id or txn.id}"
            )
            return await self.gateway.create_payment_link(
                amount_paise=txn.amount_paise,
                currency=txn.currency,
                description=description,
                customer={"name": customer.name, "email": customer.email, "phone": customer.phone},
                notes=notes,
                notify=notify,
                reference_id=attempt.idempotency_key,
            )

        if action is Action.SEND_REMINDER:
            # A reminder needs an existing link to point at; if there is none,
            # create one silently so the reminder has somewhere to send people.
            prior = next(
                (a.provider_ref for a in reversed(txn.attempts) if a.provider_ref and a.id != attempt.id),
                None,
            )
            if prior:
                return await self.gateway.send_reminder(payment_link_id=prior)
            return await self.gateway.create_payment_link(
                amount_paise=txn.amount_paise, currency=txn.currency,
                description=f"Reminder: payment pending for order {txn.razorpay_order_id or txn.id}",
                customer={"name": customer.name, "email": customer.email, "phone": customer.phone},
                notes=notes, notify=True, reference_id=attempt.idempotency_key,
            )

        if action is Action.ESCALATE:
            from ..razorpay_client.base import GatewayResult

            return GatewayResult(ok=True, reference=None, status="escalated_to_merchant")

        raise GatewayError(f"No execution path for action {action}", retryable=False)

    @staticmethod
    def _describe(action: Action, url: str | None) -> str:
        return {
            Action.RETRY: "Re-presented the payment on the original order.",
            Action.RETRY_DELAYED: "Re-presented the payment after the cooldown.",
            Action.RETRY_SUBSCRIPTION: "Re-presented the subscription charge against the mandate.",
            Action.CREATE_PAYMENT_LINK: f"Generated an alternative payment link{f' ({url})' if url else ''}.",
            Action.SEND_REMINDER: "Sent a payment reminder to the customer.",
            Action.ESCALATE: "Escalated to the merchant for a manual recovery conversation.",
        }.get(action, f"Executed {action}.")
