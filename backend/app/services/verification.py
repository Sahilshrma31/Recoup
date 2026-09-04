"""Recovery verification -- the step that turns an action into a number.

An agent that acts but never checks whether the money arrived is just an
automation script. This module closes the loop: it resolves due attempts,
marks transactions recovered, and feeds failures back into the state machine
so the agent can decide whether to try again or stop.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..enums import Action, AttemptStatus, RecoveryState
from ..models import RecoveryAttempt, Transaction, utcnow
from ..razorpay_client.base import GatewayError, PaymentGateway
from . import activity, simulator
from .simulator import Latent
from .state_machine import advance_to, transition

log = logging.getLogger(__name__)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class VerificationService:
    def __init__(self, settings: Settings, gateway: PaymentGateway) -> None:
        self.settings = settings
        self.gateway = gateway
        self._rng = random.Random()

    def due_attempts(self, db: Session, *, limit: int = 50) -> list[RecoveryAttempt]:
        now = utcnow()
        return list(
            db.execute(
                select(RecoveryAttempt)
                .where(
                    RecoveryAttempt.status == AttemptStatus.AWAITING_CUSTOMER,
                    RecoveryAttempt.resolve_at.is_not(None),
                    RecoveryAttempt.resolve_at <= now,
                )
                .order_by(RecoveryAttempt.resolve_at)
                .limit(limit)
            ).scalars().all()
        )

    async def verify(self, db: Session, attempt: RecoveryAttempt) -> bool:
        """Resolve one attempt. Returns True if the payment was recovered."""
        txn = attempt.transaction
        succeeded = (
            await self._verify_live(attempt)
            if self.settings.live_execution
            else self._verify_simulated(attempt, txn)
        )

        attempt.completed_at = utcnow()
        attempt.status = AttemptStatus.SUCCEEDED if succeeded else AttemptStatus.FAILED

        if succeeded:
            return self._mark_recovered(db, txn, attempt)
        return self._mark_failed(db, txn, attempt)

    # -- outcome sources ---------------------------------------------------

    async def _verify_live(self, attempt: RecoveryAttempt) -> bool:
        """Ask Razorpay whether the money actually arrived."""
        if not attempt.provider_ref:
            return False
        try:
            data = await self.gateway.fetch_order(attempt.provider_ref)
        except GatewayError as exc:
            log.warning("Verification call failed for %s: %s", attempt.idempotency_key, exc)
            return False
        return str(data.get("status", "")).lower() in {"paid", "captured"}

    def _verify_simulated(self, attempt: RecoveryAttempt, txn: Transaction) -> bool:
        """Sample the outcome from the hidden ground truth (offline mode).

        Two different clocks meet here and must not be confused:

        * **How old the failure is.** Real elapsed time since `failed_at`. A
          transaction seeded ten days ago really is ten days stale, and customer
          intent has really decayed that much.
        * **How long the agent chose to wait.** For the demo, one simulated
          minute is compressed into `simulated_minute_seconds` of wall clock, so
          a 10-minute cooldown elapses in ~10 seconds. Measuring that against
          the wall clock would tell the simulator the agent waited 0.2 minutes
          and the degraded rail would never have "recovered".

        The rail-recovery check takes whichever is larger, since a rail heals
        either because the agent waited or because time simply passed.
        """
        latent = Latent.from_dict(txn.latent)
        if latent is None:
            return False

        failed_at = _aware(txn.failed_at) or _aware(txn.created_at) or utcnow()
        executed_at = _aware(attempt.executed_at) or utcnow()
        scheduled_at = _aware(attempt.created_at) or executed_at

        real_age_minutes = max(0.0, (executed_at - failed_at).total_seconds() / 60.0)
        agent_wait_simulated = max(
            0.0,
            (executed_at - scheduled_at).total_seconds()
            / max(self.settings.simulated_minute_seconds, 1e-9),
        )

        succeeded, _ = simulator.resolve(
            Action(attempt.action),
            latent,
            self._rng,
            attempt_number=attempt.attempt_number,
            outreach_number=max(0, txn.outreach_count - 1),
            minutes_waited=max(real_age_minutes, agent_wait_simulated),
            days_since_failure=real_age_minutes / 1440,
        )
        return succeeded

    # -- state updates -----------------------------------------------------

    def _mark_recovered(self, db: Session, txn: Transaction, attempt: RecoveryAttempt) -> bool:
        txn.recovered_at = utcnow()
        txn.recovered_amount_paise = txn.amount_paise
        txn.status = "captured"
        txn.at_risk = False
        advance_to(txn, RecoveryState.RECOVERED)

        customer = txn.customer
        customer.successful_payments += 1
        customer.lifetime_value_paise += txn.amount_paise
        customer.last_payment_at = txn.recovered_at

        failed_at = _aware(txn.failed_at) or _aware(txn.created_at) or utcnow()
        # How stale the money was: real elapsed time, uncompressed.
        age_minutes = (txn.recovered_at - failed_at).total_seconds() / 60.0

        # How long the agent took once it acted: converted back to the simulated
        # clock, so "45 minutes for a customer to open a link" reports as 45
        # minutes and not as the 45 seconds the demo actually spent.
        first_action = min(
            (a.executed_at for a in txn.attempts if a.executed_at), default=attempt.executed_at
        )
        first_action = _aware(first_action) or txn.recovered_at
        elapsed_seconds = max(0.0, (txn.recovered_at - first_action).total_seconds())
        txn.agent_recovery_minutes = round(
            elapsed_seconds
            if self.settings.live_execution
            else elapsed_seconds / max(self.settings.simulated_minute_seconds, 1e-9),
            2,
        )

        activity.emit(
            db, transaction_id=txn.id, stage="verify", level="success",
            message=f"Rs {txn.amount_paise / 100:,.0f} RECOVERED via {attempt.action} "
                    f"in {txn.agent_recovery_minutes:.0f} min.",
            detail={"attempt_id": attempt.id, "amount_paise": txn.amount_paise,
                    "agent_recovery_minutes": txn.agent_recovery_minutes,
                    "failure_age_minutes": round(age_minutes, 1)},
        )
        return True

    def _mark_failed(self, db: Session, txn: Transaction, attempt: RecoveryAttempt) -> bool:
        if txn.recovery_state == RecoveryState.EXECUTING:
            transition(txn, RecoveryState.ATTEMPT_FAILED)
        activity.emit(
            db, transaction_id=txn.id, stage="verify", level="warn",
            message=f"{attempt.action} did not convert; re-evaluating next best action.",
            detail={"attempt_id": attempt.id},
        )
        return False
