"""Deterministic failure diagnosis (design §9).

This is the *baseline* diagnosis: a defensible, explainable classification
derived only from the observed failure code plus context. The LLM layer may
refine it (and must justify itself), but if the LLM is unavailable this is
what the agent uses -- so a model outage degrades quality, never correctness.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..enums import DiagnosisCategory, TxnKind

#: Observed failure code -> (category, cause code, base confidence).
#: Confidence below ~0.7 marks a genuinely ambiguous code: the same string can
#: come from a flaky rail or from a customer who simply cannot pay.
_REASON_MAP: dict[str, tuple[DiagnosisCategory, str, float]] = {
    "gateway_timeout": (DiagnosisCategory.TEMPORARY_TECHNICAL, "gateway_timeout", 0.93),
    "upi_timeout": (DiagnosisCategory.TEMPORARY_TECHNICAL, "upi_rail_timeout", 0.92),
    "bank_unavailable": (DiagnosisCategory.TEMPORARY_TECHNICAL, "bank_downtime", 0.94),
    "issuer_unavailable": (DiagnosisCategory.TEMPORARY_TECHNICAL, "issuer_downtime", 0.92),
    "network_error": (DiagnosisCategory.TEMPORARY_TECHNICAL, "network_error", 0.88),
    "payment_timed_out": (DiagnosisCategory.TEMPORARY_TECHNICAL, "customer_timeout", 0.72),
    "insufficient_funds": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "insufficient_funds", 0.95),
    "card_declined": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "card_declined", 0.85),
    "invalid_card": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "invalid_instrument", 0.95),
    "expired_card": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "expired_instrument", 0.97),
    "incorrect_cvv": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "credential_error", 0.90),
    "authentication_failed": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "authentication_failed", 0.75),
    "do_not_honour": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "issuer_declined_unspecified", 0.55),
    "payment_frequency_limit_exceeded": (
        DiagnosisCategory.TEMPORARY_TECHNICAL, "velocity_limit", 0.80,
    ),
    "risk_blocked": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "risk_blocked", 0.85),
    "payment_failed": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "unspecified_failure", 0.40),
    "unknown": (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "unspecified_failure", 0.35),
    "checkout_abandoned": (DiagnosisCategory.CHECKOUT_ABANDONMENT, "checkout_abandoned", 0.90),
}

_DEFAULT = (DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE, "unspecified_failure", 0.35)

#: Cause codes for which re-presenting the same instrument is pointless.
HARD_INSTRUMENT_FAILURES = frozenset(
    {"insufficient_funds", "invalid_instrument", "expired_instrument", "credential_error", "risk_blocked"}
)


@dataclass(slots=True)
class Diagnosis:
    category: DiagnosisCategory
    cause: str
    confidence: float
    rationale: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = str(self.category)
        return d


def diagnose(features: dict) -> Diagnosis:
    """Classify a failure from observable signals only."""
    reason = features.get("failure_reason") or "unknown"
    rationale: list[str] = []

    # Transaction kind wins over the raw code: a failed renewal is a
    # subscription problem regardless of which code the bank returned.
    if features.get("is_subscription"):
        category, cause, confidence = (
            DiagnosisCategory.SUBSCRIPTION_FAILURE,
            f"subscription_{_REASON_MAP.get(reason, _DEFAULT)[1]}",
            _REASON_MAP.get(reason, _DEFAULT)[2],
        )
        rationale.append("Subscription renewal charge -- routed to the subscription recovery workflow.")
    elif features.get("is_checkout_abandonment") or reason == "checkout_abandoned":
        category, cause, confidence = (
            DiagnosisCategory.CHECKOUT_ABANDONMENT, "checkout_abandoned", 0.90,
        )
        rationale.append("Order was created but no payment was ever completed.")
    else:
        category, cause, confidence = _REASON_MAP.get(reason, _DEFAULT)
        rationale.append(f"Observed failure code `{reason}` maps to {cause}.")

    # --- context modifiers -------------------------------------------------
    # A concurrent, merchant-wide failure spike on the same rail is strong
    # evidence for a temporary rail problem, even behind an ambiguous code.
    spike = float(features.get("recent_failure_spike_ratio") or 1.0)
    if spike >= 2.0 and cause not in HARD_INSTRUMENT_FAILURES:
        pct = int((spike - 1) * 100)
        if category is DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE:
            category = DiagnosisCategory.TEMPORARY_TECHNICAL
            cause = "rail_degradation"
            confidence = max(confidence, 0.78)
            rationale.append(
                f"{features.get('method', 'this method').upper()} failures are up {pct}% "
                f"in the last 10 minutes -- reclassified as rail degradation, not customer inability."
            )
        else:
            confidence = min(0.97, confidence + 0.05)
            rationale.append(f"Corroborated by a {pct}% failure spike on this rail.")

    # A customer who has paid many times before is unlikely to have suddenly
    # become unable to pay; that shifts an ambiguous decline toward technical.
    success_rate = float(features.get("customer_success_rate") or 0.0)
    prior = int(features.get("customer_previous_successful_payments") or 0)
    if prior >= 3 and success_rate >= 0.8:
        rationale.append(
            f"Customer has {prior} successful payments at a {success_rate:.0%} success rate."
        )
        if confidence < 0.6 and category is DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE:
            category = DiagnosisCategory.TEMPORARY_TECHNICAL
            cause = "likely_transient_decline"
            confidence = 0.62
            rationale.append("Ambiguous decline on a consistently-good payer treated as transient.")
    elif features.get("customer_is_new"):
        rationale.append("No payment history for this customer -- no behavioural prior available.")

    # Repeated failure with no engagement dominates everything else: this is
    # the category that tells the agent to stop.
    retries = int(features.get("retry_count") or 0)
    outreach = int(features.get("outreach_count") or 0)
    days = float(features.get("days_since_failure") or 0.0)
    if retries >= 3 or outreach >= 3 or days >= 30:
        category = DiagnosisCategory.LOW_RECOVERY_PROBABILITY
        cause = "recovery_exhausted"
        confidence = 0.90
        rationale.append(
            f"{retries} re-attempts and {outreach} outreach touches over {days:.0f} days "
            "with no conversion -- further intervention is unlikely to pay off."
        )

    return Diagnosis(category=category, cause=cause, confidence=round(confidence, 4), rationale=rationale)
