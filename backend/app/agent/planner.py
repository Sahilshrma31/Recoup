"""Action planner (design §11, §12).

Decides which actions are structurally *possible* for a transaction, scores
them, and hands the ranked list to the policy engine. Eligibility here is
about physics, not permission: you cannot re-present a charge for a checkout
that never produced a payment, and you cannot retry a subscription mandate on
a one-off order. Permission is the policy engine's job.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..enums import Action, DiagnosisCategory, TxnKind
from ..policy.guardrails import PolicyEngine, PolicyOutcome
from .diagnosis import Diagnosis
from .predictor import ScoredAction, score_actions

#: How long to wait before a delayed re-attempt, by cause. Waiting is only
#: useful when the thing you are waiting for actually recovers on its own.
_DELAY_BY_CAUSE: dict[str, int] = {
    "bank_downtime": 15,
    "issuer_downtime": 15,
    "rail_degradation": 10,
    "upi_rail_timeout": 5,
    "gateway_timeout": 5,
    "network_error": 3,
    "velocity_limit": 60,
    "likely_transient_decline": 30,
}
_DEFAULT_DELAY_MINUTES = 5


@dataclass(slots=True)
class Plan:
    diagnosis: Diagnosis
    scored: list[ScoredAction]
    outcome: PolicyOutcome
    delay_minutes: int

    @property
    def action(self) -> Action:
        return self.outcome.chosen.action

    @property
    def probability(self) -> float:
        return self.outcome.chosen.probability

    @property
    def expected_recovery_paise(self) -> int:
        return self.outcome.chosen.expected_value_paise


def retry_delay_minutes(diagnosis: Diagnosis) -> int:
    return _DELAY_BY_CAUSE.get(diagnosis.cause, _DEFAULT_DELAY_MINUTES)


def eligible_actions(features: dict, diagnosis: Diagnosis) -> list[Action]:
    """The candidate set for this transaction. NO_ACTION is always in it."""
    kind = features.get("kind")
    actions: list[Action] = [Action.NO_ACTION]

    if kind == TxnKind.SUBSCRIPTION:
        actions += [Action.RETRY_SUBSCRIPTION, Action.CREATE_PAYMENT_LINK, Action.SEND_REMINDER]
    elif kind == TxnKind.CHECKOUT or diagnosis.category is DiagnosisCategory.CHECKOUT_ABANDONMENT:
        # No payment was ever submitted, so there is nothing to re-present.
        actions += [Action.CREATE_PAYMENT_LINK, Action.SEND_REMINDER]
    else:
        actions += [
            Action.RETRY,
            Action.RETRY_DELAYED,
            Action.CREATE_PAYMENT_LINK,
            Action.SEND_REMINDER,
        ]

    # A human recovery conversation only makes economic sense on large tickets.
    if int(features.get("amount_paise") or 0) >= 5_000_000:
        actions.append(Action.ESCALATE)
    return actions


def plan(
    features: dict,
    diagnosis: Diagnosis,
    *,
    settings: Settings,
    recovery_state,
    preferred_action: Action | None = None,
    preferred_delay_minutes: int | None = None,
    minutes_since_last_attempt: float | None = None,
    merchant_approved: bool = False,
    policy_engine: PolicyEngine | None = None,
) -> Plan:
    """Score -> rank -> guard. The output is always executable."""
    engine = policy_engine or PolicyEngine(settings)
    delay = preferred_delay_minutes if preferred_delay_minutes is not None else retry_delay_minutes(diagnosis)

    candidates = eligible_actions(features, diagnosis)
    if preferred_action is not None and preferred_action not in candidates:
        # The AI asked for something structurally impossible here. Record it by
        # simply not scoring it; the policy trail will show the override.
        preferred_action = None

    scored = score_actions(features, diagnosis, candidates, retry_delay_minutes=delay)
    outcome = engine.select(
        scored,
        features=features,
        diagnosis=diagnosis,
        recovery_state=recovery_state,
        preferred=preferred_action,
        minutes_since_last_attempt=minutes_since_last_attempt,
        merchant_approved=merchant_approved,
    )
    return Plan(
        diagnosis=diagnosis,
        scored=scored,
        outcome=outcome,
        delay_minutes=delay if outcome.chosen.action is Action.RETRY_DELAYED else 0,
    )
