"""The recovery agent: Detect -> Diagnose -> Predict -> Decide -> Guard -> Act -> Verify.

The ordering here is the whole architectural argument. The LLM sits in the
middle of the pipeline, not at the top of it: it receives an already-computed
deterministic analysis, and its output is a *proposal* that the policy engine
must ratify. Money is only ever moved by `services/executor.py`, which the
model cannot call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import Settings
from ..enums import Action, DecisionSource, RecoveryState
from ..models import AgentDecision, RecoveryAttempt, Transaction, utcnow
from ..policy.guardrails import PolicyEngine
from ..services import activity
from ..services.state_machine import transition
from .diagnosis import Diagnosis, diagnose
from .features import MerchantContext, build_merchant_context, compute_features
from .llm import LLMUnavailable, ReasoningClient
from .planner import Plan, plan as build_plan

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AnalysisResult:
    decision: AgentDecision
    plan: Plan
    used_llm: bool


def _minutes_since_last_attempt(txn: Transaction) -> float | None:
    executed = [a.executed_at for a in txn.attempts if a.executed_at]
    if not executed:
        return None
    last = max(executed)
    last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 60.0


class RecoveryAgent:
    def __init__(
        self,
        settings: Settings,
        reasoning: ReasoningClient,
        *,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.settings = settings
        self.reasoning = reasoning
        self.policy = policy_engine or PolicyEngine(settings)

    async def analyse(
        self,
        db: Session,
        txn: Transaction,
        *,
        ctx: MerchantContext | None = None,
        merchant_approved: bool = False,
        use_llm: bool = True,
    ) -> AnalysisResult:
        ctx = ctx or build_merchant_context(db)

        if RecoveryState(txn.recovery_state) in (RecoveryState.DETECTED, RecoveryState.ATTEMPT_FAILED,
                                                 RecoveryState.STOPPED):
            transition(txn, RecoveryState.ANALYZING)

        # --- 1. features -------------------------------------------------
        features = compute_features(txn, txn.customer, ctx)
        activity.emit(
            db, transaction_id=txn.id, stage="detect",
            message=f"Analysing Rs {txn.amount_paise / 100:,.0f} {txn.method.upper()} failure "
                    f"({txn.failure_reason}).",
            detail={"customer_success_rate": features["customer_success_rate"],
                    "attempt_number": features["attempt_number"]},
        )

        # --- 2. deterministic diagnosis ----------------------------------
        deterministic = diagnose(features)
        activity.emit(
            db, transaction_id=txn.id, stage="diagnose",
            message=f"Rule-based diagnosis: {deterministic.cause} "
                    f"({deterministic.confidence:.0%} confidence).",
            detail={"category": str(deterministic.category), "rationale": deterministic.rationale},
        )

        # --- 3. deterministic plan (also the fallback if the model is down) --
        baseline_plan = build_plan(
            features, deterministic,
            settings=self.settings,
            recovery_state=RecoveryState(txn.recovery_state),
            minutes_since_last_attempt=_minutes_since_last_attempt(txn),
            merchant_approved=merchant_approved,
            policy_engine=self.policy,
        )
        activity.emit(
            db, transaction_id=txn.id, stage="predict",
            message=f"Recovery probability {baseline_plan.probability:.0%} for "
                    f"{baseline_plan.action}; expected recovery "
                    f"Rs {baseline_plan.expected_recovery_paise / 100:,.0f}.",
            detail={"scorecard": [s.to_dict() for s in baseline_plan.scored]},
        )

        # --- 4. AI reasoning (advisory) ----------------------------------
        analysis, llm_meta = None, None
        if use_llm and self.reasoning.available:
            try:
                result = await self.reasoning.analyse(
                    features=features,
                    diagnosis=deterministic.to_dict(),
                    scored_actions=[s.to_dict() for s in baseline_plan.scored],
                    deterministic_choice=str(baseline_plan.action),
                    policy_notes=self._policy_notes(baseline_plan),
                )
                analysis, llm_meta = result.analysis, result
            except LLMUnavailable as exc:
                activity.emit(
                    db, transaction_id=txn.id, stage="diagnose", level="warn",
                    message=f"AI reasoning unavailable ({exc}); continuing on deterministic rules.",
                )

        final_plan, diagnosis, source = baseline_plan, deterministic, DecisionSource.RULES

        if analysis is not None:
            diagnosis = Diagnosis(
                category=type(deterministic.category)(analysis.diagnosis_category),
                cause=analysis.cause,
                confidence=analysis.confidence,
                rationale=list(analysis.key_signals) or deterministic.rationale,
            )
            proposed = Action(analysis.recommended_action)
            activity.emit(
                db, transaction_id=txn.id, stage="diagnose",
                message=f"AI diagnosis: {analysis.cause} ({analysis.confidence:.0%}) -> proposes {proposed}.",
                detail={"reason": analysis.reason, "signals": analysis.key_signals,
                        "agrees_with_scorecard": analysis.agrees_with_scorecard},
            )
            # 5. The proposal is re-planned and re-guarded from scratch. The
            #    model's preference can only *reorder* candidates, never skip a check.
            final_plan = build_plan(
                features, diagnosis,
                settings=self.settings,
                recovery_state=RecoveryState(txn.recovery_state),
                preferred_action=proposed,
                preferred_delay_minutes=analysis.delay_minutes or None,
                minutes_since_last_attempt=_minutes_since_last_attempt(txn),
                merchant_approved=merchant_approved,
                policy_engine=self.policy,
            )
            source = (
                DecisionSource.LLM if final_plan.action is proposed else DecisionSource.LLM_OVERRIDDEN
            )

        # --- 6. guardrail outcome ----------------------------------------
        outcome = final_plan.outcome
        if outcome.overridden_from:
            activity.emit(
                db, transaction_id=txn.id, stage="guard", level="warn",
                message=f"Policy blocked {outcome.overridden_from} -> {final_plan.action}. "
                        f"{outcome.override_reason}",
                detail=outcome.to_dict(),
            )
        else:
            activity.emit(
                db, transaction_id=txn.id, stage="guard",
                message=f"Policy check passed for {final_plan.action}.",
                detail={"checks": [c.to_dict() for c in outcome.evaluation.checks]},
            )

        reasoning_summary = (
            analysis.reason if analysis is not None else self._deterministic_summary(diagnosis, final_plan)
        )
        if outcome.overridden_from and outcome.override_reason:
            reasoning_summary = f"{reasoning_summary} Policy override: {outcome.override_reason}"

        decision = AgentDecision(
            transaction_id=txn.id,
            diagnosis=diagnosis.cause,
            category=str(diagnosis.category),
            diagnosis_confidence=diagnosis.confidence,
            # What the agent *wanted* before guardrails: the model's proposal, or
            # (with no model) the highest-expected-value action in the scorecard.
            # Recording the post-policy choice here would hide every intervention
            # the policy engine actually made.
            recommended_action=str(
                Action(analysis.recommended_action)
                if analysis is not None
                else (baseline_plan.scored[0].action if baseline_plan.scored else baseline_plan.action)
            ),
            action=str(final_plan.action),
            delay_minutes=final_plan.delay_minutes,
            recovery_probability=final_plan.probability,
            expected_recovery_paise=final_plan.expected_recovery_paise,
            reasoning_summary=reasoning_summary,
            source=str(source),
            model=(llm_meta.model if llm_meta else None),
            latency_ms=(llm_meta.latency_ms if llm_meta else 0),
            features=features,
            action_scores=[s.to_dict() for s in final_plan.scored],
            policy_result=outcome.to_dict(),
            llm_payload=(llm_meta.input_payload if llm_meta else None),
        )
        db.add(decision)
        db.flush()

        if final_plan.action is not Action.NO_ACTION:
            if outcome.requires_approval:
                transition(txn, RecoveryState.AWAITING_APPROVAL)
                activity.emit(
                    db, transaction_id=txn.id, stage="guard", level="warn",
                    message=f"Rs {txn.amount_paise / 100:,.0f} exceeds the auto-action limit -- "
                            "waiting for merchant approval.",
                )
            else:
                transition(txn, RecoveryState.PLANNED)

        activity.emit(
            db, transaction_id=txn.id, stage="decide",
            message=f"Decision: {final_plan.action}"
                    + (f" after {final_plan.delay_minutes} min" if final_plan.delay_minutes else "")
                    + f" (p={final_plan.probability:.0%}, "
                      f"expected Rs {final_plan.expected_recovery_paise / 100:,.0f}).",
            detail={"decision_id": decision.id, "source": str(source)},
        )
        return AnalysisResult(decision=decision, plan=final_plan, used_llm=analysis is not None)

    @staticmethod
    def _policy_notes(plan: Plan) -> list[str]:
        """Tell the model which actions are already off the table, and why."""
        notes = [
            f"{e.action} is BLOCKED: {'; '.join(e.block_reasons)}"
            for e in plan.outcome.rejected
            if e.block_reasons
        ]
        if plan.outcome.requires_approval:
            notes.append(f"{plan.outcome.chosen.action} requires merchant approval before execution.")
        return notes

    @staticmethod
    def _deterministic_summary(diagnosis: Diagnosis, plan: Plan) -> str:
        lead = diagnosis.rationale[0] if diagnosis.rationale else diagnosis.cause
        return (
            f"{lead} Chose {plan.action} with a {plan.probability:.0%} recovery probability "
            f"(expected Rs {plan.expected_recovery_paise / 100:,.0f})."
        )
