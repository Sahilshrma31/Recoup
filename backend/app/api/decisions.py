"""Audit trail (design §21) -- the data behind "Why did AI do this?"."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AgentDecision
from ..schemas import DecisionOut
from ..services import activity

router = APIRouter(tags=["decisions"])


@router.get("/decisions", response_model=list[DecisionOut])
def list_decisions(
    db: Session = Depends(get_db),
    transaction_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    stmt = select(AgentDecision).order_by(AgentDecision.id.desc()).limit(limit)
    if transaction_id:
        stmt = (
            select(AgentDecision)
            .where(AgentDecision.transaction_id == transaction_id)
            .order_by(AgentDecision.id.desc())
            .limit(limit)
        )
    return list(db.execute(stmt).scalars().all())


@router.get("/decisions/{decision_id}", response_model=DecisionOut)
def get_decision(decision_id: int, db: Session = Depends(get_db)):
    decision = db.get(AgentDecision, decision_id)
    if decision is None:
        raise HTTPException(404, f"Unknown decision {decision_id}")
    return decision


@router.get("/activity")
def recent_activity(
    db: Session = Depends(get_db),
    limit: int = Query(60, ge=1, le=300),
    transaction_id: str | None = None,
):
    return activity.recent(db, limit=limit, transaction_id=transaction_id)
