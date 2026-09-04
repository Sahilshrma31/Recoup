"""Recovery execution endpoints -- the buttons the merchant actually presses."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..enums import Action, RecoveryState
from ..models import Transaction
from ..schemas import ActionResponse
from ..services import activity
from ..services.runtime import get_runtime
from ..services.state_machine import transition
from .deps import latest_decision

router = APIRouter(prefix="/recovery", tags=["recovery"])


@router.post("/{transaction_id}/recover", response_model=ActionResponse)
async def recover(transaction_id: str, db: Session = Depends(get_db)):
    """Analyse and execute in one pass -- the dashboard's [Recover Payment]."""
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(404, f"Unknown transaction {transaction_id}")
    if txn.recovered_at is not None:
        raise HTTPException(409, "Transaction has already been recovered.")

    runtime = get_runtime()
    result = await runtime.agent.analyse(db, txn)
    attempt = None
    if RecoveryState(txn.recovery_state) is not RecoveryState.AWAITING_APPROVAL:
        attempt = await runtime.executor.execute(db, txn, result.decision)
    db.commit()

    return ActionResponse(
        transaction_id=txn.id,
        action=result.decision.action,
        recovery_state=txn.recovery_state,
        decision_id=result.decision.id,
        attempt_id=(attempt.id if attempt else None),
        message=result.decision.reasoning_summary,
    )


@router.post("/{transaction_id}/approve", response_model=ActionResponse)
async def approve(transaction_id: str, db: Session = Depends(get_db)):
    """Merchant approval for an action above the auto-action limit (§29)."""
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(404, f"Unknown transaction {transaction_id}")
    if RecoveryState(txn.recovery_state) is not RecoveryState.AWAITING_APPROVAL:
        raise HTTPException(409, f"Transaction is {txn.recovery_state}, not awaiting approval.")

    runtime = get_runtime()
    activity.emit(
        db, transaction_id=txn.id, stage="guard",
        message=f"Merchant approved the recovery action for Rs {txn.amount_paise / 100:,.0f}.",
    )
    # Re-plan under approval so the decision on record is the one that ran.
    result = await runtime.agent.analyse(db, txn, merchant_approved=True)
    attempt = await runtime.executor.execute(db, txn, result.decision)
    db.commit()
    return ActionResponse(
        transaction_id=txn.id,
        action=result.decision.action,
        recovery_state=txn.recovery_state,
        decision_id=result.decision.id,
        attempt_id=(attempt.id if attempt else None),
        message="Approved and executed.",
    )


@router.post("/{transaction_id}/stop", response_model=ActionResponse)
def stop(transaction_id: str, reason: str = "merchant_stopped", db: Session = Depends(get_db)):
    """Let the merchant call off recovery for a transaction."""
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(404, f"Unknown transaction {transaction_id}")
    if RecoveryState(txn.recovery_state) is RecoveryState.RECOVERED:
        raise HTTPException(409, "Transaction has already been recovered.")
    transition(txn, RecoveryState.STOPPED, reason=reason)
    activity.emit(
        db, transaction_id=txn.id, stage="act", level="stop",
        message="Merchant stopped recovery for this transaction.",
    )
    db.commit()
    return ActionResponse(
        transaction_id=txn.id, action=str(Action.NO_ACTION),
        recovery_state=txn.recovery_state, message="Recovery stopped.",
    )
