"""Policy engine: the deterministic gate in front of every action.

The agent proposes; this module disposes. An LLM recommendation is only ever a
*candidate* -- it becomes an execution when, and only when, every rule here
passes. When the top candidate is blocked the engine walks down the ranked
list, which is what produces the design's signature moment: "retry -> blocked,
insufficient funds -> generate a payment link instead".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..enums import Action, PolicyStatus, RecoveryState
from ..agent.diagnosis import Diagnosis
from ..agent.predictor import ScoredAction
from .rules import ALL_RULES, Check, PolicyContext


@dataclass(slots=True)
class PolicyEvaluation:
    action: Action
    checks: list[Check]

    @property
    def blocked(self) -> bool:
        return any(c.blocked for c in self.checks)

    @property
    def requires_approval(self) -> bool:
        return any(c.status is PolicyStatus.REQUIRES_APPROVAL for c in self.checks)

    @property
    def allowed(self) -> bool:
        return not self.blocked

    @property
    def block_reasons(self) -> list[str]:
        return [c.detail for c in self.checks if c.blocked]

    def to_dict(self) -> dict:
        return {
            "action": str(self.action),
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "checks": [c.to_dict() for c in self.checks],
            "block_reasons": self.block_reasons,
        }


@dataclass(slots=True)
class PolicyOutcome:
    """The final, executable decision plus the full record of how it was reached."""

    chosen: ScoredAction
    evaluation: PolicyEvaluation
    overridden_from: Action | None
    override_reason: str | None
    rejected: list[PolicyEvaluation] = field(default_factory=list)

    @property
    def requires_approval(self) -> bool:
        return self.evaluation.requires_approval

    def to_dict(self) -> dict:
        return {
            "action": str(self.chosen.action),
            "requires_approval": self.requires_approval,
            "overridden_from": str(self.overridden_from) if self.overridden_from else None,
            "override_reason": self.override_reason,
            "checks": [c.to_dict() for c in self.evaluation.checks],
            "rejected": [e.to_dict() for e in self.rejected],
        }


class PolicyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        scored: ScoredAction,
        *,
        features: dict,
        diagnosis: Diagnosis,
        recovery_state: RecoveryState,
        minutes_since_last_attempt: float | None = None,
        merchant_approved: bool = False,
    ) -> PolicyEvaluation:
        ctx = PolicyContext(
            action=scored.action,
            features=features,
            diagnosis=diagnosis,
            recovery_probability=scored.probability,
            expected_recovery_paise=scored.expected_value_paise,
            recovery_state=recovery_state,
            settings=self.settings,
            minutes_since_last_attempt=minutes_since_last_attempt,
            merchant_approved=merchant_approved,
        )
        return PolicyEvaluation(action=scored.action, checks=[rule(ctx) for rule in ALL_RULES])

    def select(
        self,
        ranked: list[ScoredAction],
        *,
        features: dict,
        diagnosis: Diagnosis,
        recovery_state: RecoveryState,
        preferred: Action | None = None,
        minutes_since_last_attempt: float | None = None,
        merchant_approved: bool = False,
    ) -> PolicyOutcome:
        """Walk the ranked candidates and return the best *permitted* action.

        `preferred` is the AI's recommendation. It is honoured when it is legal
        and scored; it is never allowed to skip a check.
        """
        candidates = list(ranked)
        if preferred is not None:
            candidates.sort(key=lambda s: (s.action != preferred, -s.net_expected_value_paise))

        no_action = next((s for s in candidates if s.action is Action.NO_ACTION), None)
        rejected: list[PolicyEvaluation] = []

        for scored in candidates:
            evaluation = self.evaluate(
                scored,
                features=features,
                diagnosis=diagnosis,
                recovery_state=recovery_state,
                minutes_since_last_attempt=minutes_since_last_attempt,
                merchant_approved=merchant_approved,
            )
            if not evaluation.allowed:
                rejected.append(evaluation)
                continue

            first = candidates[0]
            overridden_from = first.action if first.action is not scored.action else None
            reason = None
            if overridden_from is not None:
                blocked_eval = next((e for e in rejected if e.action is overridden_from), None)
                reason = (
                    "; ".join(blocked_eval.block_reasons)
                    if blocked_eval
                    else f"{overridden_from} was not permitted by policy."
                )
            return PolicyOutcome(
                chosen=scored,
                evaluation=evaluation,
                overridden_from=overridden_from,
                override_reason=reason,
                rejected=rejected,
            )

        # Nothing was permitted. NO_ACTION is always a legal terminal choice:
        # the agent stops, and the audit trail records exactly why.
        fallback = no_action or ranked[-1]
        evaluation = self.evaluate(
            fallback,
            features=features,
            diagnosis=diagnosis,
            recovery_state=recovery_state,
            minutes_since_last_attempt=minutes_since_last_attempt,
            merchant_approved=merchant_approved,
        )
        first_action = candidates[0].action if candidates else None
        blocked_eval = next((e for e in rejected if e.action is first_action), None)
        return PolicyOutcome(
            chosen=fallback,
            evaluation=evaluation,
            overridden_from=first_action if first_action is not fallback.action else None,
            override_reason=(
                "; ".join(blocked_eval.block_reasons)
                if blocked_eval
                else "Every recovery action was blocked by policy."
            ),
            rejected=rejected,
        )
