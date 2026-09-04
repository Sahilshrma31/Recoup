"""Ground-truth outcome model for offline execution.

Why this exists and why it is built the way it is
-------------------------------------------------
To claim "the agent recovers more revenue than a fixed retry policy" you need
outcomes, and outcomes need either production traffic or a simulator. The risk
with a simulator is circularity: if outcomes are generated from the same table
the agent scores with, the experiment proves nothing.

So this module is deliberately structured differently from `agent/predictor.py`:

* Outcomes are driven by a **latent true cause** and a per-customer
  **willingness to pay** that the agent never observes.
* What the agent *does* observe is a **noisy emission** of that latent cause --
  `do_not_honour` can come from a flaky bank or from an empty account, and
  `unknown` can come from anything.

The agent's task is therefore genuine inference under uncertainty, and the
experiment measures whether it infers better than a fixed policy. It is still a
simulator, not production data, and the README says so plainly.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass

from ..enums import Action


@dataclass(slots=True)
class Latent:
    """Hidden state. Never read by any module under `app/agent` or `app/policy`."""

    true_cause: str
    willingness: float          # 0-1: how much this customer still wants to pay
    rail_recovery_minutes: int  # how long a transient rail problem lasts

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict | None) -> "Latent | None":
        if not d:
            return None
        return Latent(
            true_cause=d.get("true_cause", "unknown"),
            willingness=float(d.get("willingness", 0.5)),
            rail_recovery_minutes=int(d.get("rail_recovery_minutes", 0)),
        )


#: Latent cause -> observed failure code distribution. The overlaps are the
#: point: several causes emit the same ambiguous code.
OBSERVATION_MODEL: dict[str, list[tuple[str, float]]] = {
    "temp_rail": [
        ("upi_timeout", 0.32), ("gateway_timeout", 0.20), ("network_error", 0.13),
        ("payment_failed", 0.18), ("unknown", 0.17),
    ],
    "temp_bank": [
        ("bank_unavailable", 0.30), ("issuer_unavailable", 0.20), ("do_not_honour", 0.20),
        ("payment_failed", 0.15), ("unknown", 0.15),
    ],
    "funds_transient": [
        ("insufficient_funds", 0.58), ("do_not_honour", 0.22),
        ("payment_failed", 0.12), ("unknown", 0.08),
    ],
    "funds_persistent": [
        ("insufficient_funds", 0.70), ("do_not_honour", 0.16),
        ("payment_failed", 0.09), ("unknown", 0.05),
    ],
    "dead_instrument": [
        ("expired_card", 0.28), ("invalid_card", 0.27), ("card_declined", 0.27),
        ("do_not_honour", 0.12), ("incorrect_cvv", 0.06),
    ],
    "risk_block": [
        ("risk_blocked", 0.48), ("card_declined", 0.20),
        ("do_not_honour", 0.20), ("authentication_failed", 0.12),
    ],
    "velocity_block": [
        ("payment_frequency_limit_exceeded", 0.55), ("do_not_honour", 0.25), ("payment_failed", 0.20),
    ],
    "customer_distracted": [("checkout_abandoned", 0.80), ("payment_timed_out", 0.20)],
    "customer_disengaged": [("checkout_abandoned", 0.85), ("payment_timed_out", 0.15)],
    "mandate_transient": [
        ("bank_unavailable", 0.30), ("insufficient_funds", 0.35),
        ("do_not_honour", 0.20), ("payment_failed", 0.15),
    ],
    "mandate_dead": [
        ("invalid_card", 0.35), ("expired_card", 0.35), ("card_declined", 0.30),
    ],
}

#: True cause -> base success probability per action, before fatigue/staleness.
#: `None` means the action is structurally impossible for that cause.
_BASE: dict[str, dict[Action, float | None]] = {
    "temp_rail": {
        Action.RETRY: 0.22, Action.RETRY_DELAYED: 0.88, Action.CREATE_PAYMENT_LINK: 0.55,
        Action.SEND_REMINDER: 0.34, Action.RETRY_SUBSCRIPTION: 0.70, Action.ESCALATE: 0.45,
    },
    "temp_bank": {
        Action.RETRY: 0.18, Action.RETRY_DELAYED: 0.84, Action.CREATE_PAYMENT_LINK: 0.52,
        Action.SEND_REMINDER: 0.30, Action.RETRY_SUBSCRIPTION: 0.66, Action.ESCALATE: 0.42,
    },
    "funds_transient": {
        Action.RETRY: 0.09, Action.RETRY_DELAYED: 0.16, Action.CREATE_PAYMENT_LINK: 0.60,
        Action.SEND_REMINDER: 0.44, Action.RETRY_SUBSCRIPTION: 0.18, Action.ESCALATE: 0.40,
    },
    "funds_persistent": {
        Action.RETRY: 0.03, Action.RETRY_DELAYED: 0.05, Action.CREATE_PAYMENT_LINK: 0.16,
        Action.SEND_REMINDER: 0.11, Action.RETRY_SUBSCRIPTION: 0.05, Action.ESCALATE: 0.14,
    },
    "dead_instrument": {
        Action.RETRY: 0.02, Action.RETRY_DELAYED: 0.02, Action.CREATE_PAYMENT_LINK: 0.68,
        Action.SEND_REMINDER: 0.28, Action.RETRY_SUBSCRIPTION: 0.04, Action.ESCALATE: 0.45,
    },
    "risk_block": {
        Action.RETRY: 0.04, Action.RETRY_DELAYED: 0.06, Action.CREATE_PAYMENT_LINK: 0.24,
        Action.SEND_REMINDER: 0.10, Action.RETRY_SUBSCRIPTION: 0.05, Action.ESCALATE: 0.52,
    },
    "velocity_block": {
        Action.RETRY: 0.05, Action.RETRY_DELAYED: 0.62, Action.CREATE_PAYMENT_LINK: 0.44,
        Action.SEND_REMINDER: 0.26, Action.RETRY_SUBSCRIPTION: 0.48, Action.ESCALATE: 0.38,
    },
    "customer_distracted": {
        Action.RETRY: 0.05, Action.RETRY_DELAYED: 0.06, Action.CREATE_PAYMENT_LINK: 0.62,
        Action.SEND_REMINDER: 0.52, Action.RETRY_SUBSCRIPTION: None, Action.ESCALATE: 0.40,
    },
    "customer_disengaged": {
        Action.RETRY: 0.02, Action.RETRY_DELAYED: 0.02, Action.CREATE_PAYMENT_LINK: 0.14,
        Action.SEND_REMINDER: 0.09, Action.RETRY_SUBSCRIPTION: None, Action.ESCALATE: 0.16,
    },
    "mandate_transient": {
        Action.RETRY: 0.12, Action.RETRY_DELAYED: 0.30, Action.CREATE_PAYMENT_LINK: 0.50,
        Action.SEND_REMINDER: 0.30, Action.RETRY_SUBSCRIPTION: 0.74, Action.ESCALATE: 0.44,
    },
    "mandate_dead": {
        Action.RETRY: 0.02, Action.RETRY_DELAYED: 0.03, Action.CREATE_PAYMENT_LINK: 0.40,
        Action.SEND_REMINDER: 0.22, Action.RETRY_SUBSCRIPTION: 0.06, Action.ESCALATE: 0.34,
    },
}

_TRANSIENT_RAIL_CAUSES = frozenset({"temp_rail", "temp_bank", "velocity_block", "mandate_transient"})

#: Actions whose success depends on the customer choosing to act.
_CUSTOMER_DEPENDENT = frozenset(
    {Action.CREATE_PAYMENT_LINK, Action.SEND_REMINDER, Action.ESCALATE}
)

#: How long until the outcome of an action is known, in simulated minutes.
_RESOLUTION_MINUTES: dict[Action, tuple[int, int]] = {
    Action.RETRY: (1, 3),
    Action.RETRY_DELAYED: (1, 3),
    Action.RETRY_SUBSCRIPTION: (1, 4),
    Action.CREATE_PAYMENT_LINK: (4, 90),
    Action.SEND_REMINDER: (20, 300),
    Action.ESCALATE: (60, 480),
    Action.NO_ACTION: (0, 0),
}


def observe_failure_reason(true_cause: str, rng: random.Random) -> str:
    """Emit the noisy failure code a merchant would actually see."""
    options = OBSERVATION_MODEL.get(true_cause)
    if not options:
        return "unknown"
    codes, weights = zip(*options)
    return rng.choices(codes, weights=weights, k=1)[0]


def success_probability(
    action: Action,
    latent: Latent,
    *,
    attempt_number: int = 1,
    outreach_number: int = 0,
    minutes_waited: float = 0.0,
    days_since_failure: float = 0.0,
) -> float:
    """True probability this action converts. The agent never sees this."""
    if action is Action.NO_ACTION:
        # Some customers come back on their own; more of them if they wanted to pay.
        return max(0.0, 0.05 * latent.willingness * math.exp(-days_since_failure / 10))

    base = _BASE.get(latent.true_cause, {}).get(action)
    if base is None:
        return 0.0
    p = float(base)

    # A transient rail heals on a clock. Acting before it has healed mostly fails;
    # waiting long enough is most of the value of RETRY_DELAYED.
    if latent.true_cause in _TRANSIENT_RAIL_CAUSES and action in (
        Action.RETRY, Action.RETRY_DELAYED, Action.RETRY_SUBSCRIPTION
    ):
        if minutes_waited < latent.rail_recovery_minutes:
            progress = minutes_waited / max(latent.rail_recovery_minutes, 1)
            p = 0.05 + (p - 0.05) * (progress ** 2)

    # Anything the customer must act on is gated by whether they still want to.
    if action in _CUSTOMER_DEPENDENT:
        p *= 0.35 + 0.65 * latent.willingness

    p *= 0.78 ** max(0, attempt_number - 1)   # attempt fatigue
    p *= 0.70 ** max(0, outreach_number)      # message fatigue
    p *= math.exp(-days_since_failure / 16)   # intent decays with staleness
    return max(0.0, min(0.98, p))


def resolve(
    action: Action,
    latent: Latent,
    rng: random.Random,
    *,
    attempt_number: int = 1,
    outreach_number: int = 0,
    minutes_waited: float = 0.0,
    days_since_failure: float = 0.0,
) -> tuple[bool, float]:
    """Sample an outcome. Returns (succeeded, true_probability)."""
    p = success_probability(
        action, latent,
        attempt_number=attempt_number, outreach_number=outreach_number,
        minutes_waited=minutes_waited, days_since_failure=days_since_failure,
    )
    return rng.random() < p, p


def resolution_minutes(action: Action, rng: random.Random) -> int:
    lo, hi = _RESOLUTION_MINUTES.get(action, (1, 5))
    return rng.randint(lo, hi) if hi > lo else lo
