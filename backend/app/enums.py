"""Shared vocabulary: the bounded action space, states and failure taxonomy.

The action space is deliberately small (design §12). An agent that can only
choose from six actions is far easier to reason about -- and to guard -- than
one that can call arbitrary payment APIs.
"""
from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    RETRY = "RETRY"                              # re-attempt now on the same order
    RETRY_DELAYED = "RETRY_DELAYED"              # re-attempt after a cooldown
    CREATE_PAYMENT_LINK = "CREATE_PAYMENT_LINK"  # alternative payment route
    SEND_REMINDER = "SEND_REMINDER"              # nudge, no new payment instrument
    RETRY_SUBSCRIPTION = "RETRY_SUBSCRIPTION"    # subscription charge re-attempt
    ESCALATE = "ESCALATE"                        # hand to a human / merchant ops
    NO_ACTION = "NO_ACTION"                      # deliberate stop


#: Actions that move money and therefore need an idempotency key.
FINANCIAL_ACTIONS = frozenset(
    {Action.RETRY, Action.RETRY_DELAYED, Action.CREATE_PAYMENT_LINK, Action.RETRY_SUBSCRIPTION}
)

#: Actions that contact the customer and count against outreach limits.
OUTREACH_ACTIONS = frozenset({Action.CREATE_PAYMENT_LINK, Action.SEND_REMINDER})

#: Actions that count against the retry limit.
RETRY_ACTIONS = frozenset({Action.RETRY, Action.RETRY_DELAYED, Action.RETRY_SUBSCRIPTION})


class DiagnosisCategory(StrEnum):
    """Design §9. The category drives which actions are even considered."""

    TEMPORARY_TECHNICAL = "A_TEMPORARY_TECHNICAL"
    CUSTOMER_PAYMENT_ISSUE = "B_CUSTOMER_PAYMENT_ISSUE"
    CHECKOUT_ABANDONMENT = "C_CHECKOUT_ABANDONMENT"
    SUBSCRIPTION_FAILURE = "D_SUBSCRIPTION_FAILURE"
    LOW_RECOVERY_PROBABILITY = "E_LOW_RECOVERY_PROBABILITY"


class RecoveryState(StrEnum):
    """Design §15."""

    DETECTED = "DETECTED"
    ANALYZING = "ANALYZING"
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    RECOVERED = "RECOVERED"
    STOPPED = "STOPPED"


#: Legal transitions. Anything else raises -- the state machine is the
#: workflow's source of truth, not a status string someone happens to write.
STATE_TRANSITIONS: dict[RecoveryState, frozenset[RecoveryState]] = {
    RecoveryState.DETECTED: frozenset({RecoveryState.ANALYZING, RecoveryState.STOPPED}),
    RecoveryState.ANALYZING: frozenset(
        {RecoveryState.PLANNED, RecoveryState.AWAITING_APPROVAL, RecoveryState.STOPPED}
    ),
    RecoveryState.PLANNED: frozenset(
        {RecoveryState.EXECUTING, RecoveryState.AWAITING_APPROVAL, RecoveryState.STOPPED}
    ),
    RecoveryState.AWAITING_APPROVAL: frozenset(
        {RecoveryState.EXECUTING, RecoveryState.PLANNED, RecoveryState.STOPPED}
    ),
    RecoveryState.EXECUTING: frozenset(
        {RecoveryState.RECOVERED, RecoveryState.ATTEMPT_FAILED, RecoveryState.STOPPED}
    ),
    RecoveryState.ATTEMPT_FAILED: frozenset({RecoveryState.ANALYZING, RecoveryState.STOPPED}),
    RecoveryState.RECOVERED: frozenset(),
    RecoveryState.STOPPED: frozenset({RecoveryState.ANALYZING}),  # merchant may re-open
}

TERMINAL_STATES = frozenset({RecoveryState.RECOVERED, RecoveryState.STOPPED})


class TxnStatus(StrEnum):
    CAPTURED = "captured"
    FAILED = "failed"
    CREATED = "created"      # order created, checkout not completed
    ATTEMPTED = "attempted"  # order has >=1 failed payment attempt


class TxnKind(StrEnum):
    PAYMENT = "payment"
    CHECKOUT = "checkout"
    SUBSCRIPTION = "subscription"


class AttemptStatus(StrEnum):
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    AWAITING_CUSTOMER = "awaiting_customer"  # link sent / reminder sent
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING_MANUAL = "pending_manual"        # provider unreachable, needs merchant
    BLOCKED = "blocked"


class DecisionSource(StrEnum):
    LLM = "llm"
    RULES = "rules"                    # deterministic fallback (LLM down/refused)
    LLM_OVERRIDDEN = "llm_overridden"  # LLM proposal rejected by the policy engine


class PolicyStatus(StrEnum):
    PASSED = "passed"
    BLOCKED = "blocked"
    REQUIRES_APPROVAL = "requires_approval"


#: Observed failure codes (Razorpay-style). Note that several are genuinely
#: ambiguous -- `do_not_honour`, `payment_failed`, `unknown` -- which is exactly
#: where context-aware reasoning beats a static lookup table.
FAILURE_REASONS = (
    "gateway_timeout",
    "upi_timeout",
    "bank_unavailable",
    "issuer_unavailable",
    "network_error",
    "payment_timed_out",
    "insufficient_funds",
    "card_declined",
    "invalid_card",
    "expired_card",
    "incorrect_cvv",
    "authentication_failed",
    "do_not_honour",
    "payment_frequency_limit_exceeded",
    "risk_blocked",
    "payment_failed",
    "unknown",
    "checkout_abandoned",
)
