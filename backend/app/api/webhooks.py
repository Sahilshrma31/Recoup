"""Razorpay webhook ingestion (design §25).

The endpoint does as little as possible: verify, record, enqueue, return 200
fast. All reasoning happens on the worker, so a slow model can never make the
payment provider's webhook time out and retry.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..enums import RecoveryState, TxnKind, TxnStatus
from ..models import Customer, Transaction, utcnow
from ..razorpay_client.webhooks import InvalidSignature, verify_signature
from ..services import activity
from ..services.runtime import RecoveryEvent, get_runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Razorpay event -> the internal event the recovery worker understands.
_HANDLED = {
    "payment.failed": "payment.failed",
    "order.paid": "payment.captured",
    "payment.captured": "payment.captured",
    "subscription.charged": "subscription.charged",
    "subscription.pending": "subscription.charge.failed",
    "subscription.halted": "subscription.charge.failed",
    "payment_link.paid": "payment.captured",
}


def _extract(payload: dict) -> tuple[str | None, dict]:
    """Pull the payment/order entity out of Razorpay's nested envelope."""
    entities = payload.get("payload", {})
    for key in ("payment", "order", "subscription", "payment_link"):
        entity = entities.get(key, {}).get("entity")
        if entity:
            return key, entity
    return None, {}


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_razorpay_signature: str | None = Header(default=None),
):
    body = await request.body()
    try:
        verify_signature(body, x_razorpay_signature, settings)
    except InvalidSignature as exc:
        raise HTTPException(401, str(exc)) from exc

    payload = await request.json()
    event_name = payload.get("event", "")
    internal = _HANDLED.get(event_name)
    if internal is None:
        return {"status": "ignored", "event": event_name}

    _, entity = _extract(payload)
    payment_id = entity.get("id")
    if not payment_id:
        raise HTTPException(400, "Webhook payload carried no entity id.")

    txn = db.get(Transaction, payment_id)

    if internal == "payment.captured":
        # Money arrived, possibly through a recovery link. Close the loop.
        if txn is not None and txn.recovered_at is None:
            txn.status = TxnStatus.CAPTURED
            txn.recovered_at = utcnow()
            txn.recovered_amount_paise = txn.amount_paise
            txn.at_risk = False
            txn.recovery_state = str(RecoveryState.RECOVERED)
            activity.emit(
                db, transaction_id=txn.id, stage="verify", level="success",
                message=f"Rs {txn.amount_paise / 100:,.0f} confirmed captured by webhook.",
            )
            db.commit()
        return {"status": "ok", "event": event_name}

    if txn is None:
        txn = _ingest(db, entity, payload, internal)
        if txn is None:
            raise HTTPException(400, "Could not ingest the failed payment from this payload.")

    activity.emit(
        db, transaction_id=txn.id, stage="detect",
        message=f"Webhook `{event_name}` received -- queued for recovery analysis.",
    )
    db.commit()

    await get_runtime().publish(RecoveryEvent(type=internal, transaction_id=txn.id))
    return {"status": "queued", "event": event_name, "transaction_id": txn.id}


def _ingest(db: Session, entity: dict, payload: dict, internal: str) -> Transaction | None:
    """Create a transaction for a payment we have not seen before."""
    amount = entity.get("amount")
    if amount is None:
        return None

    contact = str(entity.get("contact") or "unknown")
    email = str(entity.get("email") or f"{contact}@unknown.invalid")
    customer_id = f"cust_wh_{abs(hash(email)) % 10_000_000}"
    customer = db.get(Customer, customer_id)
    if customer is None:
        customer = Customer(
            id=customer_id, name=entity.get("notes", {}).get("name", "Unknown customer"),
            email=email, phone=contact,
        )
        db.add(customer)
        db.flush()

    kind = TxnKind.SUBSCRIPTION if internal.startswith("subscription") else TxnKind.PAYMENT
    txn = Transaction(
        id=entity["id"],
        razorpay_payment_id=entity.get("id"),
        razorpay_order_id=entity.get("order_id"),
        customer_id=customer.id,
        amount_paise=int(amount),
        currency=entity.get("currency", "INR"),
        method=entity.get("method", "unknown"),
        card_type=(entity.get("card", {}) or {}).get("type"),
        bank=entity.get("bank"),
        kind=kind,
        status=TxnStatus.FAILED,
        failure_reason=(
            entity.get("error_reason")
            or entity.get("error_code")
            or entity.get("error_description")
            or "unknown"
        ),
        subscription_id=entity.get("subscription_id"),
        failed_at=utcnow(),
        recovery_state=str(RecoveryState.DETECTED),
        at_risk=True,
    )
    db.add(txn)
    db.flush()
    return txn
