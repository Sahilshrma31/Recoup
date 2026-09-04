"""Revenue metrics (design §27).

Every definition here is written down explicitly, because "recovery rate" is
the kind of number that quietly means five different things. The dashboard
shows these, and the README repeats the definitions.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..enums import Action, AttemptStatus, RecoveryState, RETRY_ACTIONS, TxnStatus
from ..models import AgentDecision, RecoveryAttempt, Transaction


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _latest_decision_subquery():
    """Most recent decision per transaction."""
    return (
        select(
            AgentDecision.transaction_id.label("txn_id"),
            func.max(AgentDecision.id).label("decision_id"),
        )
        .group_by(AgentDecision.transaction_id)
        .subquery()
    )


def overview(db: Session) -> dict:
    """Headline numbers.

    revenue_at_risk    -- total value of every failed / abandoned transaction.
    revenue_recovered  -- value actually collected by a recovery action.
    estimated_recoverable -- recovered, plus (amount x recovery probability)
                          for open transactions the agent has already scored.
                          It is a forecast, and it is labelled as one.
    recovery_rate      -- recovered / estimated_recoverable.
    """
    at_risk_total = db.execute(
        select(func.coalesce(func.sum(Transaction.amount_paise), 0)).where(
            Transaction.status != TxnStatus.CAPTURED
        )
    ).scalar_one()
    recovered_total = db.execute(
        select(func.coalesce(func.sum(Transaction.recovered_amount_paise), 0))
    ).scalar_one()

    at_risk_total += recovered_total  # a recovered transaction was still exposure

    latest = _latest_decision_subquery()
    open_forecast = db.execute(
        select(
            func.coalesce(
                func.sum(AgentDecision.expected_recovery_paise), 0
            )
        )
        .select_from(Transaction)
        .join(latest, latest.c.txn_id == Transaction.id)
        .join(AgentDecision, AgentDecision.id == latest.c.decision_id)
        .where(Transaction.recovered_at.is_(None), Transaction.at_risk.is_(True))
    ).scalar_one()

    recoverable = recovered_total + open_forecast

    open_count = db.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.status != TxnStatus.CAPTURED, Transaction.recovered_at.is_(None)
        )
    ).scalar_one()
    recovered_count = db.execute(
        select(func.count()).select_from(Transaction).where(Transaction.recovered_at.is_not(None))
    ).scalar_one()
    unanalysed = db.execute(
        select(func.count()).select_from(Transaction)
        .outerjoin(latest, latest.c.txn_id == Transaction.id)
        .where(Transaction.at_risk.is_(True), latest.c.decision_id.is_(None))
    ).scalar_one()

    return {
        "revenue_at_risk_paise": int(at_risk_total),
        "estimated_recoverable_paise": int(recoverable),
        "revenue_recovered_paise": int(recovered_total),
        "recovery_rate": round(recovered_total / recoverable, 4) if recoverable else 0.0,
        "at_risk_transactions": int(open_count),
        "recovered_transactions": int(recovered_count),
        "pending_analysis": int(unanalysed),
    }


def pipeline(db: Session) -> list[dict]:
    """Current recommended action across every open at-risk transaction."""
    latest = _latest_decision_subquery()
    rows = db.execute(
        select(
            AgentDecision.action,
            func.count().label("count"),
            func.coalesce(func.sum(Transaction.amount_paise), 0).label("value"),
        )
        .select_from(Transaction)
        .join(latest, latest.c.txn_id == Transaction.id)
        .join(AgentDecision, AgentDecision.id == latest.c.decision_id)
        .where(Transaction.recovered_at.is_(None))
        .group_by(AgentDecision.action)
        .order_by(func.count().desc())
    ).all()
    return [
        {"action": r.action, "count": int(r.count), "value_paise": int(r.value)} for r in rows
    ]


def failure_breakdown(db: Session) -> list[dict]:
    rows = db.execute(
        select(
            Transaction.failure_reason,
            func.count().label("count"),
            func.coalesce(func.sum(Transaction.amount_paise), 0).label("value"),
            func.sum(case((Transaction.recovered_at.is_not(None), 1), else_=0)).label("recovered"),
        )
        .where(Transaction.status != TxnStatus.CAPTURED, Transaction.failure_reason.is_not(None))
        .group_by(Transaction.failure_reason)
        .order_by(func.count().desc())
        .limit(12)
    ).all()
    return [
        {
            "failure_reason": r.failure_reason,
            "count": int(r.count),
            "value_paise": int(r.value),
            "recovered": int(r.recovered or 0),
            "recovery_rate": round(float(r.recovered or 0) / r.count, 4) if r.count else 0.0,
        }
        for r in rows
    ]


def performance(db: Session) -> dict:
    """Quality metrics.

    action_precision  -- share of completed recovery attempts that converted.
    false_retry_rate  -- share of executed retries that did not convert. This is
                         the number that punishes an agent for retrying blindly.
    manual_intervention_rate -- share of at-risk transactions that needed a
                         human (approval gate, escalation, or provider failure).
    """
    completed = db.execute(
        select(
            func.count().label("total"),
            func.sum(case((RecoveryAttempt.status == AttemptStatus.SUCCEEDED, 1), else_=0)).label("won"),
        ).where(RecoveryAttempt.status.in_([AttemptStatus.SUCCEEDED, AttemptStatus.FAILED]))
    ).one()

    retry_actions = [str(a) for a in RETRY_ACTIONS]
    retries = db.execute(
        select(
            func.count().label("total"),
            func.sum(case((RecoveryAttempt.status == AttemptStatus.FAILED, 1), else_=0)).label("lost"),
        ).where(
            RecoveryAttempt.action.in_(retry_actions),
            RecoveryAttempt.status.in_([AttemptStatus.SUCCEEDED, AttemptStatus.FAILED]),
        )
    ).one()

    # Two recovery-time numbers, because they answer different questions.
    # `avg_recovery_minutes` is failure -> money (design §27), which on a seeded
    # backlog is dominated by how long the transaction sat there before anyone
    # looked at it. `avg_agent_recovery_minutes` is first-action -> money, which
    # is the part the agent is actually responsible for.
    recovered = db.execute(
        select(
            Transaction.failed_at,
            Transaction.created_at,
            Transaction.recovered_at,
            func.min(RecoveryAttempt.executed_at).label("first_action_at"),
        )
        .outerjoin(RecoveryAttempt, RecoveryAttempt.transaction_id == Transaction.id)
        .where(Transaction.recovered_at.is_not(None))
        .group_by(Transaction.id)
    ).all()
    durations, agent_durations = [], []
    for failed_at, created_at, recovered_at, first_action_at in recovered:
        start = _aware(failed_at) or _aware(created_at)
        end = _aware(recovered_at)
        if start and end and end >= start:
            durations.append((end - start).total_seconds() / 60.0)
        acted = _aware(first_action_at)
        if acted and end and end >= acted:
            agent_durations.append((end - acted).total_seconds() / 60.0)

    manual = db.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.recovery_state == RecoveryState.AWAITING_APPROVAL
        )
    ).scalar_one()
    manual += db.execute(
        select(func.count(func.distinct(RecoveryAttempt.transaction_id))).where(
            RecoveryAttempt.status == AttemptStatus.PENDING_MANUAL
        )
    ).scalar_one()
    at_risk_count = db.execute(
        select(func.count()).select_from(Transaction).where(Transaction.status != TxnStatus.CAPTURED)
    ).scalar_one() or 1

    stopped = db.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.recovery_state == RecoveryState.STOPPED
        )
    ).scalar_one()

    return {
        "attempts_completed": int(completed.total or 0),
        "action_precision": round(float(completed.won or 0) / completed.total, 4) if completed.total else 0.0,
        "retries_executed": int(retries.total or 0),
        "false_retry_rate": round(float(retries.lost or 0) / retries.total, 4) if retries.total else 0.0,
        "avg_recovery_minutes": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "avg_agent_recovery_minutes": (
            round(sum(agent_durations) / len(agent_durations), 1) if agent_durations else 0.0
        ),
        "manual_intervention_rate": round(manual / at_risk_count, 4),
        "deliberately_stopped": int(stopped),
    }


def ai_decision_stats(db: Session) -> dict:
    """How often the model and the policy engine actually disagreed."""
    rows = db.execute(
        select(AgentDecision.source, func.count()).group_by(AgentDecision.source)
    ).all()
    by_source = {r[0]: int(r[1]) for r in rows}
    overrides = db.execute(
        select(func.count()).select_from(AgentDecision).where(
            AgentDecision.recommended_action != AgentDecision.action
        )
    ).scalar_one()
    total = sum(by_source.values())
    return {
        "decisions": total,
        "by_source": by_source,
        "policy_overrides": int(overrides),
        "override_rate": round(overrides / total, 4) if total else 0.0,
    }
