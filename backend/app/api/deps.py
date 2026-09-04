"""Shared API helpers: serialisation and lookups."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentDecision, Transaction
from ..schemas import (
    AttemptOut, CustomerOut, DecisionOut, TransactionDetail, TransactionSummary, rupees,
)


def latest_decision(db: Session, transaction_id: str) -> AgentDecision | None:
    return db.execute(
        select(AgentDecision)
        .where(AgentDecision.transaction_id == transaction_id)
        .order_by(AgentDecision.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def summarise(txn: Transaction, decision: AgentDecision | None = None) -> TransactionSummary:
    return TransactionSummary(
        id=txn.id,
        customer_id=txn.customer_id,
        customer_name=txn.customer.name,
        amount_paise=txn.amount_paise,
        amount_rupees=rupees(txn.amount_paise),
        currency=txn.currency,
        method=txn.method,
        kind=txn.kind,
        status=txn.status,
        failure_reason=txn.failure_reason,
        recovery_state=txn.recovery_state,
        at_risk=txn.at_risk,
        retry_count=txn.retry_count,
        outreach_count=txn.outreach_count,
        failed_at=txn.failed_at,
        recovered_at=txn.recovered_at,
        recovered_amount_paise=txn.recovered_amount_paise,
        recommended_action=(decision.action if decision else None),
        recovery_probability=(decision.recovery_probability if decision else None),
        expected_recovery_paise=(decision.expected_recovery_paise if decision else None),
        diagnosis=(decision.diagnosis if decision else None),
    )


def detail(txn: Transaction, activity_rows: list[dict]) -> TransactionDetail:
    latest = txn.decisions[-1] if txn.decisions else None
    base = summarise(txn, latest)
    return TransactionDetail(
        **base.model_dump(),
        customer=CustomerOut.model_validate(txn.customer),
        bank=txn.bank,
        card_type=txn.card_type,
        subscription_id=txn.subscription_id,
        stop_reason=txn.stop_reason,
        decisions=[DecisionOut.model_validate(d) for d in reversed(txn.decisions)],
        attempts=[AttemptOut.model_validate(a) for a in txn.attempts],
        activity=activity_rows,
    )
