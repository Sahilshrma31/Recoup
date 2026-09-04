"""Demo controls: simulate failures, run the queue, reset state.

These endpoints exist so the system can be *shown* without waiting for real
traffic. They are separated into their own router so a production deployment
can simply not mount it.
"""
from __future__ import annotations

import asyncio
import random
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..enums import RecoveryState, TxnKind, TxnStatus
from ..models import (
    ActivityEvent, AgentDecision, Customer, RecoveryAttempt, Transaction, utcnow,
)
from ..services import activity
from ..services.runtime import RecoveryEvent, get_runtime
from ..services.simulator import Latent, observe_failure_reason

router = APIRouter(prefix="/demo", tags=["demo"])

DEMO_SCENARIOS = {
    "bank_outage": "pay_demo_upi",
    "card_declined": "pay_demo_card",
    "stop": "pay_demo_stop",
}


@router.post("/simulate/failure")
async def simulate_failure(
    db: Session = Depends(get_db),
    amount_rupees: int = Body(7500, embed=True),
    method: str = Body("upi", embed=True),
    true_cause: str = Body("temp_bank", embed=True),
    customer_id: str | None = Body(None, embed=True),
):
    """Inject a brand-new failed payment and hand it to the agent.

    `true_cause` sets the hidden ground truth, so you can demonstrate a case
    where the agent has to infer the cause from an ambiguous code.
    """
    rng = random.Random()
    customer = (
        db.get(Customer, customer_id)
        if customer_id
        else db.execute(select(Customer).order_by(Customer.successful_payments.desc()).limit(1)).scalar_one_or_none()
    )
    if customer is None:
        raise HTTPException(400, "No customers in the database -- run the seed script first.")

    txn_id = f"pay_sim_{rng.randint(1_000_000, 9_999_999)}"
    txn = Transaction(
        id=txn_id, razorpay_payment_id=txn_id, razorpay_order_id=f"order_sim_{txn_id[-7:]}",
        customer_id=customer.id, amount_paise=amount_rupees * 100, currency="INR", method=method,
        card_type=("credit" if method == "card" else None), bank="HDFC",
        kind=TxnKind.PAYMENT, status=TxnStatus.FAILED,
        failure_reason=observe_failure_reason(true_cause, rng),
        failed_at=utcnow(), recovery_state=str(RecoveryState.DETECTED), at_risk=True,
        latent=Latent(
            true_cause=true_cause,
            willingness=round(rng.uniform(0.55, 0.95), 3),
            rail_recovery_minutes=(rng.randint(4, 12) if true_cause.startswith(("temp", "velocity", "mandate_trans")) else 0),
        ).to_dict(),
    )
    db.add(txn)
    activity.emit(
        db, transaction_id=txn.id, stage="detect",
        message=f"Payment failure detected: Rs {amount_rupees:,} {method.upper()} "
                f"({txn.failure_reason}).",
    )
    db.commit()

    await get_runtime().publish(RecoveryEvent(type="payment.failed", transaction_id=txn.id, source="demo"))
    return {
        "transaction_id": txn.id,
        "observed_failure_reason": txn.failure_reason,
        "hidden_true_cause": true_cause,  # shown here only to make the demo legible
        "status": "queued",
    }


@router.post("/scenario/{name}")
async def run_scenario(name: str, db: Session = Depends(get_db)):
    """Reset and run one of the three pinned scenarios from the design doc."""
    txn_id = DEMO_SCENARIOS.get(name)
    if txn_id is None:
        raise HTTPException(404, f"Unknown scenario `{name}`. Try: {', '.join(DEMO_SCENARIOS)}")
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404, "Demo rows are missing -- re-run the seed script with --demo.")

    _reset_transaction(db, txn)
    db.commit()
    await get_runtime().publish(RecoveryEvent(type="payment.failed", transaction_id=txn.id, source="demo"))
    return {"scenario": name, "transaction_id": txn.id, "status": "queued"}


@router.post("/run-queue")
async def run_queue(
    db: Session = Depends(get_db),
    limit: int = Query(25, ge=1, le=500),
    order: str = Query("recent", pattern="^(recent|value)$"),
):
    """Push unanalysed at-risk transactions through the agent.

    Defaults to newest-first, which is what a recovery agent actually does: a
    failure is most recoverable in the minutes after it happens, and working a
    three-week-old backlog by value mostly produces (correct, but undramatic)
    decisions to stop. `order=value` works the backlog highest-value first.

    This is the same code path a Razorpay webhook takes.
    """
    analysed = select(AgentDecision.transaction_id).distinct()
    sort_key = (
        Transaction.failed_at.desc() if order == "recent" else Transaction.amount_paise.desc()
    )
    rows = db.execute(
        select(Transaction)
        .where(
            Transaction.at_risk.is_(True),
            Transaction.recovered_at.is_(None),
            Transaction.id.not_in(analysed),
        )
        .order_by(sort_key)
        .limit(limit)
    ).scalars().all()

    runtime = get_runtime()
    for txn in rows:
        await runtime.publish(RecoveryEvent(type="payment.failed", transaction_id=txn.id, source="demo"))
    return {"queued": len(rows), "queue_depth": runtime.queue.qsize()}


@router.post("/tick")
async def tick():
    """Advance the scheduler by one tick (execute due retries, verify outcomes)."""
    runtime = get_runtime()
    await runtime._tick()
    return {"status": "ticked", **runtime.status()}


@router.post("/reset")
def reset(db: Session = Depends(get_db), scope: str = Query("decisions", pattern="^(decisions|all)$")):
    """Clear agent output so a demo can be re-run from a clean slate.

    `decisions` wipes decisions/attempts/activity and returns at-risk
    transactions to DETECTED. `all` additionally clears recovered state.
    """
    db.query(ActivityEvent).delete()
    db.query(RecoveryAttempt).delete()   # attempts reference decisions
    db.query(AgentDecision).delete()

    txns = db.execute(
        select(Transaction).where(Transaction.status != TxnStatus.CAPTURED)
    ).scalars().all()
    for txn in txns:
        txn.recovery_state = str(RecoveryState.DETECTED)
        txn.at_risk = True
        txn.stop_reason = None
        txn.retry_count = 0
        txn.outreach_count = 0

    if scope == "all":
        for txn in db.execute(select(Transaction).where(Transaction.recovered_at.is_not(None))).scalars():
            if txn.id.startswith(("pay_demo", "pay_sim", "pay_burst")):
                txn.recovered_at = None
                txn.recovered_amount_paise = 0
                txn.status = str(TxnStatus.FAILED)
                txn.at_risk = True
                txn.recovery_state = str(RecoveryState.DETECTED)
    db.commit()

    from ..agent.features import invalidate_context_cache

    invalidate_context_cache()
    return {"status": "reset", "scope": scope, "transactions_reset": len(txns)}


def _reset_transaction(db: Session, txn: Transaction) -> None:
    db.query(ActivityEvent).filter(ActivityEvent.transaction_id == txn.id).delete()
    db.query(RecoveryAttempt).filter(RecoveryAttempt.transaction_id == txn.id).delete()
    db.query(AgentDecision).filter(AgentDecision.transaction_id == txn.id).delete()
    txn.recovery_state = str(RecoveryState.DETECTED)
    txn.at_risk = True
    txn.recovered_at = None
    txn.recovered_amount_paise = 0
    txn.status = str(TxnStatus.FAILED)
    txn.stop_reason = None
    # The "stop" scenario only makes sense with its history of failed attempts.
    if txn.id == "pay_demo_stop":
        txn.retry_count, txn.outreach_count = 2, 3
    else:
        txn.retry_count, txn.outreach_count = 0, 0
    # Move the failure to "just now" so the demo reads as live.
    if txn.id != "pay_demo_stop":
        txn.failed_at = utcnow() - timedelta(minutes=2)
