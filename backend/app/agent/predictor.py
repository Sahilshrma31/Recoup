"""Recovery-probability model (design §10).

    P(action succeeds | transaction, customer, failure context)

Implemented as a transparent additive scorecard rather than a black box, for
three reasons: every number it produces can be shown to the merchant, it needs
no training data to be useful on day one, and it is a drop-in seam -- swap
`score_actions` for a trained model and the rest of the agent is unchanged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..enums import Action, DiagnosisCategory
from .diagnosis import HARD_INSTRUMENT_FAILURES, Diagnosis

#: Starting score for each action, in points out of 100.
_BASE_SCORE: dict[Action, float] = {
    Action.RETRY: 45.0,
    Action.RETRY_DELAYED: 50.0,
    Action.CREATE_PAYMENT_LINK: 45.0,
    Action.SEND_REMINDER: 35.0,
    Action.RETRY_SUBSCRIPTION: 45.0,
    Action.ESCALATE: 30.0,
    Action.NO_ACTION: 0.0,
}

#: What it costs the merchant to take the action, in paise. Non-zero costs are
#: what let NO_ACTION win on its own economics instead of by special-casing.
ACTION_COST_PAISE: dict[Action, int] = {
    Action.RETRY: 200,
    Action.RETRY_DELAYED: 200,
    Action.CREATE_PAYMENT_LINK: 400,
    Action.SEND_REMINDER: 300,
    Action.RETRY_SUBSCRIPTION: 200,
    Action.ESCALATE: 15_000,      # ~Rs 150 of human time
    Action.NO_ACTION: 0,
}

#: Additional penalty per prior customer touch, charged to outreach actions.
#: This is the price of customer fatigue, and it compounds.
_FATIGUE_COST_PAISE = 5_000

_RETRY_LIKE = (Action.RETRY, Action.RETRY_DELAYED, Action.RETRY_SUBSCRIPTION)
_OUTREACH_LIKE = (Action.CREATE_PAYMENT_LINK, Action.SEND_REMINDER)

#: Scores are squashed through a logistic curve rather than clipped, so
#: stacked positive evidence compresses instead of piling up at "97% certain".
#: 50 points -> 0.50, 80 -> 0.80, 110 -> 0.94, 0 -> 0.10, -100 -> 0.001.
_LOGISTIC_MIDPOINT = 50.0
_LOGISTIC_SCALE = 22.0
_PROB_FLOOR, _PROB_CEILING = 0.01, 0.95


def _to_probability(score: float) -> float:
    """Map an additive point score onto a calibrated-looking probability."""
    z = (score - _LOGISTIC_MIDPOINT) / _LOGISTIC_SCALE
    prob = 1.0 / (1.0 + math.exp(-z)) if -60 < z < 60 else (0.0 if z <= 0 else 1.0)
    return max(_PROB_FLOOR, min(_PROB_CEILING, prob))


@dataclass(slots=True)
class ScoredAction:
    action: Action
    probability: float
    delay_minutes: int
    expected_value_paise: int          # amount x probability
    action_cost_paise: int
    net_expected_value_paise: int      # expected value minus cost of acting
    factors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": str(self.action),
            "probability": round(self.probability, 4),
            "delay_minutes": self.delay_minutes,
            "expected_value_paise": self.expected_value_paise,
            "action_cost_paise": self.action_cost_paise,
            "net_expected_value_paise": self.net_expected_value_paise,
            "factors": self.factors,
        }


def _add(factors: list[dict], label: str, delta: float) -> float:
    if abs(delta) < 0.01:
        return 0.0
    factors.append({"label": label, "delta": round(delta, 2)})
    return delta


def _shared_factors(features: dict, diagnosis: Diagnosis, factors: list[dict]) -> float:
    """Signals that move every action's odds in the same direction."""
    score = 0.0
    prior_ok = int(features.get("customer_previous_successful_payments") or 0)
    success_rate = float(features.get("customer_success_rate") or 0.0)

    if prior_ok:
        score += _add(factors, f"{prior_ok} previous successful payment(s)", min(prior_ok, 10) * 1.6)
    if prior_ok >= 3 and success_rate >= 0.8:
        score += _add(factors, f"High customer success rate ({success_rate:.0%})", 10.0)
    elif prior_ok + int(features.get("customer_previous_failed_payments") or 0) >= 3 and success_rate < 0.4:
        score += _add(factors, f"Low customer success rate ({success_rate:.0%})", -14.0)
    if features.get("customer_is_new"):
        score += _add(factors, "No prior payment history", -8.0)

    retries = int(features.get("retry_count") or 0)
    if retries:
        score += _add(factors, f"{retries} previous re-attempt(s) already failed", -13.0 * retries)
    outreach = int(features.get("outreach_count") or 0)
    if outreach:
        score += _add(factors, f"{outreach} previous outreach touch(es) ignored", -10.0 * outreach)

    days = float(features.get("days_since_failure") or 0.0)
    if days >= 30:
        score += _add(factors, f"{days:.0f} days overdue", -30.0)
    elif days >= 14:
        score += _add(factors, f"{days:.0f} days overdue", -22.0)
    elif days >= 7:
        score += _add(factors, f"{days:.0f} days overdue", -14.0)

    hist = float(features.get("historical_recovery_rate") or 0.0)
    if hist > 0:
        score += _add(
            factors,
            f"Historical recovery rate for `{features.get('failure_reason')}` is {hist:.0%}",
            (hist - 0.5) * 18.0,
        )

    if diagnosis.category is DiagnosisCategory.LOW_RECOVERY_PROBABILITY:
        score += _add(factors, "Diagnosis: recovery options exhausted", -25.0)
    return score


def _action_factors(action: Action, features: dict, diagnosis: Diagnosis, factors: list[dict]) -> float:
    """Signals specific to *this* action -- where the model earns its keep."""
    score = 0.0
    cat = diagnosis.category
    spike = float(features.get("recent_failure_spike_ratio") or 1.0)
    hard_instrument = diagnosis.cause in HARD_INSTRUMENT_FAILURES

    if action in _RETRY_LIKE:
        if cat is DiagnosisCategory.TEMPORARY_TECHNICAL:
            score += _add(factors, "Temporary technical failure -- the same route may now work", 17.0)
        if hard_instrument:
            score += _add(
                factors, "Re-presenting the same instrument cannot fix this failure", -34.0
            )
        if cat is DiagnosisCategory.CHECKOUT_ABANDONMENT:
            score += _add(factors, "Nothing to re-attempt: no payment was ever submitted", -30.0)
        if spike >= 2.0:
            if action is Action.RETRY:
                score += _add(factors, "Rail is degraded right now -- an immediate retry rides the outage", -11.0)
            else:
                score += _add(factors, "Delay lets the degraded rail recover before re-attempting", 13.0)
        if action is Action.RETRY_DELAYED and cat is DiagnosisCategory.TEMPORARY_TECHNICAL:
            score += _add(factors, "Cooldown improves odds on transient failures", 5.0)
        if action is Action.RETRY_SUBSCRIPTION and features.get("is_subscription"):
            score += _add(factors, "Mandate is still active -- charge can be re-presented", 8.0)

    if action in _OUTREACH_LIKE:
        if features.get("customer_opted_out"):
            score += _add(factors, "Customer has opted out of contact", -40.0)
        clv = int(features.get("customer_lifetime_value_paise") or 0)
        if clv >= 5_000_000:
            score += _add(factors, "High lifetime-value customer is more likely to respond", 6.0)

    if action is Action.CREATE_PAYMENT_LINK:
        if cat is DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE:
            score += _add(
                factors, "An alternative payment route side-steps the failing instrument", 19.0
            )
        if cat is DiagnosisCategory.CHECKOUT_ABANDONMENT:
            score += _add(factors, "Abandoned checkout converts well on a direct link", 14.0)
        if cat is DiagnosisCategory.TEMPORARY_TECHNICAL:
            score += _add(factors, "Adds customer friction where the original route would likely work", -7.0)
        if int(features.get("amount_paise") or 0) > 2_500_000:
            score += _add(factors, "Large ticket size lowers link conversion", -6.0)

    if action is Action.SEND_REMINDER:
        if cat is DiagnosisCategory.CHECKOUT_ABANDONMENT:
            score += _add(factors, "Customer intent existed but checkout was never finished", 16.0)
        if cat is DiagnosisCategory.TEMPORARY_TECHNICAL:
            score += _add(factors, "A reminder does not address a rail-side failure", -12.0)
        if cat is DiagnosisCategory.CUSTOMER_PAYMENT_ISSUE:
            score += _add(factors, "A reminder alone gives the customer no alternative route", -8.0)

    if action is Action.ESCALATE:
        if int(features.get("amount_paise") or 0) >= 5_000_000:
            score += _add(factors, "Ticket size justifies a human recovery conversation", 22.0)
        if cat is DiagnosisCategory.SUBSCRIPTION_FAILURE:
            score += _add(factors, "Subscription churn risk warrants human follow-up", 8.0)
    return score


def score_action(action: Action, features: dict, diagnosis: Diagnosis, *, delay_minutes: int = 0) -> ScoredAction:
    amount = int(features.get("amount_paise") or 0)

    if action is Action.NO_ACTION:
        # Not "0% chance of money": the honest baseline is the chance the
        # customer comes back unprompted, which is not zero for good payers.
        organic = 6.0 + min(int(features.get("customer_previous_successful_payments") or 0), 10) * 0.8
        if float(features.get("days_since_failure") or 0.0) >= 14:
            organic *= 0.4   # an old failure rarely self-heals
        prob = max(_PROB_FLOOR, min(0.20, organic / 100))
        ev = int(amount * prob)
        return ScoredAction(
            action=action,
            probability=prob,
            delay_minutes=0,
            expected_value_paise=ev,
            action_cost_paise=0,
            net_expected_value_paise=ev,
            factors=[
                {"label": "Baseline chance the customer returns unprompted", "delta": round(organic, 2)}
            ],
        )

    factors: list[dict] = []
    score = _BASE_SCORE[action]
    factors.append({"label": f"Base score for {action}", "delta": score})
    score += _shared_factors(features, diagnosis, factors)
    score += _action_factors(action, features, diagnosis, factors)

    prob = _to_probability(score)
    ev = int(amount * prob)

    cost = ACTION_COST_PAISE[action]
    if action in _OUTREACH_LIKE:
        cost += _FATIGUE_COST_PAISE * int(features.get("outreach_count") or 0)

    return ScoredAction(
        action=action,
        probability=prob,
        delay_minutes=delay_minutes,
        expected_value_paise=ev,
        action_cost_paise=cost,
        net_expected_value_paise=ev - cost,
        factors=factors,
    )


def score_actions(
    features: dict, diagnosis: Diagnosis, actions: list[Action], *, retry_delay_minutes: int = 5
) -> list[ScoredAction]:
    """Score every candidate action, best net expected value first."""
    scored = [
        score_action(
            a,
            features,
            diagnosis,
            delay_minutes=retry_delay_minutes if a is Action.RETRY_DELAYED else 0,
        )
        for a in actions
    ]
    scored.sort(key=lambda s: s.net_expected_value_paise, reverse=True)
    return scored
