"""Individual guardrail rules (design §13, §29).

Each rule is a small pure function over a `PolicyContext`. They are pure and
independently testable on purpose: these are the checks that stand between a
language model and somebody's money, so they must be readable by a human who
does not trust the model at all.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Settings
from ..enums import (
    OUTREACH_ACTIONS,
    RETRY_ACTIONS,
    Action,
    PolicyStatus,
    RecoveryState,
)
from ..agent.diagnosis import HARD_INSTRUMENT_FAILURES, Diagnosis


@dataclass(slots=True)
class PolicyContext:
    action: Action
    features: dict
    diagnosis: Diagnosis
    recovery_probability: float
    expected_recovery_paise: int
    recovery_state: RecoveryState
    settings: Settings
    minutes_since_last_attempt: float | None = None
    merchant_approved: bool = False


@dataclass(slots=True)
class Check:
    name: str
    status: PolicyStatus
    detail: str

    @property
    def blocked(self) -> bool:
        return self.status is PolicyStatus.BLOCKED

    def to_dict(self) -> dict:
        return {"name": self.name, "status": str(self.status), "detail": self.detail}


def _ok(name: str, detail: str) -> Check:
    return Check(name, PolicyStatus.PASSED, detail)


def _block(name: str, detail: str) -> Check:
    return Check(name, PolicyStatus.BLOCKED, detail)


def _approve(name: str, detail: str) -> Check:
    return Check(name, PolicyStatus.REQUIRES_APPROVAL, detail)


# --- rules ----------------------------------------------------------------


def terminal_state(ctx: PolicyContext) -> Check:
    if ctx.recovery_state is RecoveryState.RECOVERED:
        return _block("terminal_state", "Transaction is already recovered; no further action.")
    return _ok("terminal_state", "Transaction is still open for recovery.")


def attempt_limit(ctx: PolicyContext) -> Check:
    limit = ctx.settings.max_auto_retries
    used = int(ctx.features.get("retry_count") or 0)
    if ctx.action in RETRY_ACTIONS and used >= limit:
        return _block("attempt_limit", f"{used} automatic re-attempts already made (limit {limit}).")
    return _ok("attempt_limit", f"{used}/{limit} automatic re-attempts used.")


def outreach_limit(ctx: PolicyContext) -> Check:
    limit = ctx.settings.max_outreach_attempts
    used = int(ctx.features.get("outreach_count") or 0)
    if ctx.action in OUTREACH_ACTIONS and used >= limit:
        return _block("outreach_limit", f"{used} outreach touches already sent (limit {limit}).")
    return _ok("outreach_limit", f"{used}/{limit} outreach touches used.")


def customer_opt_out(ctx: PolicyContext) -> Check:
    if ctx.action in OUTREACH_ACTIONS and ctx.features.get("customer_opted_out"):
        return _block("customer_opt_out", "Customer has opted out of payment communication.")
    return _ok("customer_opt_out", "Customer has not opted out of contact.")


def futile_retry(ctx: PolicyContext) -> Check:
    """The rule from design §13: never re-present an instrument that cannot work.

    Retrying a card that was declined for insufficient funds burns a gateway
    attempt, risks an issuer velocity block, and cannot succeed.
    """
    if ctx.action in RETRY_ACTIONS and ctx.diagnosis.cause in HARD_INSTRUMENT_FAILURES:
        return _block(
            "futile_retry",
            f"Retry blocked: `{ctx.diagnosis.cause}` cannot be resolved by re-presenting "
            "the same instrument.",
        )
    return _ok("futile_retry", "Failure cause does not rule out a re-attempt.")


def probability_floor(ctx: PolicyContext) -> Check:
    floor = ctx.settings.min_recovery_probability
    if ctx.action is Action.NO_ACTION:
        return _ok("probability_floor", "Not applicable to NO_ACTION.")
    if ctx.recovery_probability < floor:
        return _block(
            "probability_floor",
            f"Recovery probability {ctx.recovery_probability:.0%} is below the {floor:.0%} floor.",
        )
    return _ok("probability_floor", f"Recovery probability {ctx.recovery_probability:.0%} clears the floor.")


def expected_value_floor(ctx: PolicyContext) -> Check:
    floor = ctx.settings.min_expected_value_paise
    if ctx.action is Action.NO_ACTION:
        return _ok("expected_value_floor", "Not applicable to NO_ACTION.")
    if ctx.expected_recovery_paise < floor:
        return _block(
            "expected_value_floor",
            f"Expected recovery Rs {ctx.expected_recovery_paise / 100:,.0f} is below the "
            f"Rs {floor / 100:,.0f} floor -- not worth the attempt.",
        )
    return _ok(
        "expected_value_floor",
        f"Expected recovery Rs {ctx.expected_recovery_paise / 100:,.0f} justifies acting.",
    )


def recovery_window(ctx: PolicyContext) -> Check:
    days = float(ctx.features.get("days_since_failure") or 0.0)
    limit = ctx.settings.recovery_window_days
    if ctx.action is not Action.NO_ACTION and days > limit:
        return _block("recovery_window", f"Failure is {days:.0f} days old (window {limit} days).")
    return _ok("recovery_window", f"Within the {limit}-day recovery window ({days:.1f} days old).")


def retry_cooldown(ctx: PolicyContext) -> Check:
    gap = ctx.settings.retry_cooldown_minutes
    since = ctx.minutes_since_last_attempt
    if ctx.action is Action.RETRY and since is not None and since < gap:
        return _block(
            "retry_cooldown",
            f"Last attempt was {since:.1f} min ago; immediate retry needs a {gap} min gap.",
        )
    return _ok("retry_cooldown", "Cooldown between attempts respected.")


def amount_limit(ctx: PolicyContext) -> Check:
    """Above the auto-action limit the agent may decide but not act alone."""
    limit = ctx.settings.auto_action_limit_paise
    amount = int(ctx.features.get("amount_paise") or 0)
    if ctx.action is Action.NO_ACTION or amount <= limit:
        return _ok("amount_limit", f"Rs {amount / 100:,.0f} is within the auto-action limit.")
    if ctx.merchant_approved:
        return _ok("amount_limit", f"Rs {amount / 100:,.0f} exceeds the limit but the merchant approved it.")
    return _approve(
        "amount_limit",
        f"Rs {amount / 100:,.0f} exceeds the Rs {limit / 100:,.0f} auto-action limit -- "
        "merchant approval required.",
    )


#: Evaluated in order; every rule always runs so the audit trail is complete.
ALL_RULES: tuple[Callable[[PolicyContext], Check], ...] = (
    terminal_state,
    attempt_limit,
    outreach_limit,
    customer_opt_out,
    futile_retry,
    probability_floor,
    expected_value_floor,
    recovery_window,
    retry_cooldown,
    amount_limit,
)
