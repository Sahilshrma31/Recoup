"""Prompt construction and the LLM's structured output contract.

Two rules shape everything here:

1. **The model reasons; it does not compute.** Amounts, probabilities and
   policy outcomes are calculated deterministically and *given* to the model.
   It is asked to interpret them, not to produce them.
2. **The model sees the minimum.** `sanitise_for_llm` is an allowlist, so a
   new column on `Transaction` can never silently start leaking to a third
   party (design §31).
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from ..enums import Action, DiagnosisCategory

#: Feature keys the model is allowed to see. Everything else -- names, emails,
#: phone numbers, card details, internal ids -- never leaves the process.
_LLM_FEATURE_ALLOWLIST = (
    "amount_rupees",
    "currency",
    "method",
    "card_type",
    "kind",
    "failure_reason",
    "is_subscription",
    "is_checkout_abandonment",
    "customer_previous_successful_payments",
    "customer_previous_failed_payments",
    "customer_success_rate",
    "customer_is_new",
    "customer_opted_out",
    "days_since_last_payment",
    "attempt_number",
    "retry_count",
    "outreach_count",
    "minutes_since_failure",
    "days_since_failure",
    "payment_method_failure_rate",
    "merchant_failure_rate",
    "recent_failure_spike_ratio",
    "recent_failure_spike",
    "historical_recovery_rate",
)

_ACTION_VALUES = tuple(str(a) for a in Action)
_CATEGORY_VALUES = tuple(str(c) for c in DiagnosisCategory)


class AgentAnalysis(BaseModel):
    """The only shape the agent will accept back from the model.

    Anything that does not validate is discarded and the deterministic
    decision stands -- a malformed model response can never reach execution.
    """

    diagnosis_category: Literal[
        "A_TEMPORARY_TECHNICAL",
        "B_CUSTOMER_PAYMENT_ISSUE",
        "C_CHECKOUT_ABANDONMENT",
        "D_SUBSCRIPTION_FAILURE",
        "E_LOW_RECOVERY_PROBABILITY",
    ] = Field(description="Failure category this transaction belongs to.")
    cause: str = Field(
        max_length=60,
        description="snake_case cause code, e.g. bank_downtime, insufficient_funds, rail_degradation.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the diagnosis, 0-1.")
    recommended_action: Literal[
        "RETRY",
        "RETRY_DELAYED",
        "CREATE_PAYMENT_LINK",
        "SEND_REMINDER",
        "RETRY_SUBSCRIPTION",
        "ESCALATE",
        "NO_ACTION",
    ] = Field(description="Single best next action from the allowed action space.")
    delay_minutes: int = Field(
        ge=0, le=1440, description="Minutes to wait before acting. 0 unless the action is RETRY_DELAYED."
    )
    agrees_with_scorecard: bool = Field(
        description="True if this matches the deterministic recommendation supplied in the context."
    )
    reason: str = Field(
        max_length=400,
        description="One or two sentences a merchant would understand, citing the specific evidence.",
    )
    key_signals: list[str] = Field(
        default_factory=list, max_length=4, description="Up to 4 short evidence bullets."
    )
    customer_message: str = Field(
        default="",
        max_length=280,
        description="Customer-facing copy. Empty unless the action contacts the customer.",
    )


SYSTEM_PROMPT = f"""\
You are the reasoning layer of an automated revenue-recovery agent for an Indian \
payments platform. A payment has failed or a checkout was abandoned. Your job is to \
work out *why*, and to recommend the single best next action.

## What you are and are not responsible for

You do NOT calculate money, probabilities, retry counts or eligibility. Those are \
computed deterministically and given to you in the context. You interpret that \
evidence and choose an action. Never invent a number that is not in the context.

Your recommendation is a proposal. A deterministic policy engine runs after you and \
can veto it. Do not try to argue around limits, and never recommend an action in \
order to bypass a constraint.

## Action space (choose exactly one)

- RETRY: re-present the same payment on the same order, immediately. Only sensible \
when the failure was transient AND the rail is healthy right now.
- RETRY_DELAYED: same, after `delay_minutes`. The right choice when a rail or issuer \
is currently degraded and is expected to recover shortly.
- CREATE_PAYMENT_LINK: send the customer an alternative payment route. The right \
choice when the original instrument cannot succeed (insufficient funds, dead card) \
but the customer probably still wants to pay.
- SEND_REMINDER: a nudge with no new payment route. Only useful when the blocker is \
customer attention, not the payment rail or the instrument.
- RETRY_SUBSCRIPTION: re-present a subscription charge against an active mandate.
- ESCALATE: hand to a human. Reserve for large tickets where automation has run out.
- NO_ACTION: deliberately stop. This is a real answer, not a failure. Choose it when \
further attempts would burn customer goodwill for very little expected value.

## Diagnosis categories

A_TEMPORARY_TECHNICAL - timeouts, bank/gateway/issuer downtime, network errors.
B_CUSTOMER_PAYMENT_ISSUE - insufficient funds, declined/expired/invalid instrument.
C_CHECKOUT_ABANDONMENT - order created, payment never submitted.
D_SUBSCRIPTION_FAILURE - a renewal charge failed.
E_LOW_RECOVERY_PROBABILITY - repeatedly failed, ignored, or long overdue.

## How to reason well

Failure codes lie. `do_not_honour`, `payment_failed` and `unknown` are ambiguous: the \
same code covers a flaky bank rail and a customer who cannot pay. Resolve the \
ambiguity with context you were given:

- A merchant-wide failure spike on the same method, concurrent with this failure, is \
strong evidence of a rail problem rather than customer inability.
- A customer with a long successful payment history rarely becomes unable to pay \
overnight; for them an ambiguous decline leans transient.
- A customer with no history and a hard decline leans toward the instrument.
- Repeated failed attempts and ignored outreach are the strongest signal that more \
attempts will not work. Recommend NO_ACTION rather than a third reminder.

You are given a deterministic scorecard with a probability and expected value per \
action. Treat it as a strong prior. Disagree with it only when a contextual signal it \
does not model justifies that, and say so plainly in `reason`. Set \
`agrees_with_scorecard` honestly.

Write `reason` for a merchant, not an engineer: name the evidence, not the feature. \
Only fill `customer_message` for CREATE_PAYMENT_LINK or SEND_REMINDER; keep it \
polite, specific and free of blame.
"""


def sanitise_for_llm(features: dict) -> dict:
    """Allowlist filter -- the only path by which data reaches the model."""
    return {k: features[k] for k in _LLM_FEATURE_ALLOWLIST if k in features}


def build_user_message(
    *,
    safe_features: dict,
    diagnosis: dict,
    scored_actions: list[dict],
    deterministic_choice: str,
    policy_notes: list[str],
) -> str:
    payload = {
        "transaction_context": safe_features,
        "deterministic_diagnosis": diagnosis,
        "scorecard": [
            {
                "action": s["action"],
                "recovery_probability": s["probability"],
                "expected_recovery_rupees": round(s["expected_value_paise"] / 100, 2),
                "net_expected_value_rupees": round(s["net_expected_value_paise"] / 100, 2),
                "why": [f["label"] for f in s.get("factors", [])],
            }
            for s in scored_actions
        ],
        "deterministic_recommendation": deterministic_choice,
        "active_policy_constraints": policy_notes,
    }
    return (
        "Analyse this failed payment and recommend one action.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}"
    )
