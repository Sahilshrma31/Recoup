"""Transaction browsing and the per-transaction agent trigger."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..enums import RecoveryState, TxnStatus
from ..models import AgentDecision, Customer, Transaction
from ..schemas import TransactionDetail, TransactionSummary
from ..services import activity
from ..services.runtime import get_runtime
from .deps import detail, latest_decision, summarise

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=dict)
def list_transactions(
    db: Session = Depends(get_db),
    status: Literal["at_risk", "recovered", "stopped", "all"] = "at_risk",
    action: str | None = None,
    method: str | None = None,
    search: str | None = None,
    sort: Literal["value", "recent", "probability"] = "value",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Paginated at-risk queue, annotated with each transaction's latest decision."""
    latest_ids = (
        select(func.max(AgentDecision.id).label("decision_id"), AgentDecision.transaction_id.label("txn_id"))
        .group_by(AgentDecision.transaction_id)
        .subquery()
    )
    stmt = (
        select(Transaction, AgentDecision)
        .options(selectinload(Transaction.customer))
        .outerjoin(latest_ids, latest_ids.c.txn_id == Transaction.id)
        .outerjoin(AgentDecision, AgentDecision.id == latest_ids.c.decision_id)
    )

    if status == "at_risk":
        stmt = stmt.where(Transaction.at_risk.is_(True), Transaction.recovered_at.is_(None))
    elif status == "recovered":
        stmt = stmt.where(Transaction.recovered_at.is_not(None))
    elif status == "stopped":
        stmt = stmt.where(Transaction.recovery_state == RecoveryState.STOPPED)
    else:
        stmt = stmt.where(Transaction.status != TxnStatus.CAPTURED)

    if action:
        stmt = stmt.where(AgentDecision.action == action)
    if method:
        stmt = stmt.where(Transaction.method == method)
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.join(Customer, Customer.id == Transaction.customer_id).where(
            or_(
                func.lower(Transaction.id).like(term),
                func.lower(Customer.name).like(term),
                func.lower(Customer.email).like(term),
            )
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    order = {
        "value": desc(Transaction.amount_paise),
        "recent": desc(Transaction.failed_at),
        "probability": desc(AgentDecision.recovery_probability),
    }[sort]
    rows = db.execute(stmt.order_by(order).limit(limit).offset(offset)).all()

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [summarise(txn, decision).model_dump() for txn, decision in rows],
    }


@router.get("/{transaction_id}", response_model=TransactionDetail)
def get_transaction(transaction_id: str, db: Session = Depends(get_db)):
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(404, f"Unknown transaction {transaction_id}")
    return detail(txn, activity.recent(db, limit=50, transaction_id=transaction_id))


@router.post("/{transaction_id}/analyze", response_model=TransactionDetail)
async def analyze_transaction(transaction_id: str, db: Session = Depends(get_db)):
    """Run the agent's analysis without executing anything.

    This is the "explain yourself" endpoint: it produces a full decision and
    audit record, but stops short of touching the payment network.
    """
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(404, f"Unknown transaction {transaction_id}")
    runtime = get_runtime()
    await runtime.agent.analyse(db, txn)
    db.commit()
    db.refresh(txn)
    return detail(txn, activity.recent(db, limit=50, transaction_id=transaction_id))
