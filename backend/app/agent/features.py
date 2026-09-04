"""Feature engine (design §8).

Turns a raw transaction row into the structured, numeric view that every
downstream stage -- deterministic scorer, policy engine and LLM -- shares.
Nothing here calls an LLM, and nothing here reads `Transaction.latent`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..enums import TxnKind, TxnStatus
from ..models import Customer, Transaction

_SPIKE_WINDOW_MINUTES = 10
_BASELINE_WINDOW_HOURS = 24
_CTX_TTL_SECONDS = 15.0
#: A spike claim needs enough traffic behind it. Without this floor a single
#: failed card payment in a quiet 10-minute window reads as a 100% failure rate
#: and the agent starts blaming the rail for what is really one bad card.
_MIN_SPIKE_SAMPLE = 8


@dataclass(slots=True)
class MerchantContext:
    """Merchant-wide signals shared across transactions, cached briefly.

    `recent_failure_spike` is what lets the agent tell "this customer cannot
    pay" apart from "the bank rail is having a bad ten minutes".
    """

    method_failure_rate: dict[str, float] = field(default_factory=dict)
    method_spike_ratio: dict[str, float] = field(default_factory=dict)
    reason_recovery_rate: dict[str, float] = field(default_factory=dict)
    merchant_failure_rate: float = 0.0
    computed_at: float = field(default_factory=time.monotonic)

    def is_stale(self) -> bool:
        return time.monotonic() - self.computed_at > _CTX_TTL_SECONDS


_cached_context: MerchantContext | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; normalise to UTC-aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_merchant_context(db: Session, *, use_cache: bool = True) -> MerchantContext:
    global _cached_context
    if use_cache and _cached_context and not _cached_context.is_stale():
        return _cached_context

    now = _utcnow()
    baseline_from = now - timedelta(hours=_BASELINE_WINDOW_HOURS)
    spike_from = now - timedelta(minutes=_SPIKE_WINDOW_MINUTES)

    # Per-method failure rate over the baseline window.
    rows = db.execute(
        select(
            Transaction.method,
            func.count().label("total"),
            func.sum(case((Transaction.status == TxnStatus.FAILED, 1), else_=0)).label("failed"),
        )
        .where(Transaction.created_at >= baseline_from)
        .group_by(Transaction.method)
    ).all()
    method_failure_rate = {
        r.method: (float(r.failed or 0) / r.total if r.total else 0.0) for r in rows
    }
    total_all = sum(r.total for r in rows)
    failed_all = sum(int(r.failed or 0) for r in rows)
    merchant_failure_rate = failed_all / total_all if total_all else 0.0

    # Failure-rate spike: last 10 minutes vs the baseline window, per method.
    spike_rows = db.execute(
        select(
            Transaction.method,
            func.count().label("total"),
            func.sum(case((Transaction.status == TxnStatus.FAILED, 1), else_=0)).label("failed"),
        )
        .where(Transaction.created_at >= spike_from)
        .group_by(Transaction.method)
    ).all()
    method_spike_ratio: dict[str, float] = {}
    for r in spike_rows:
        if not r.total or r.total < _MIN_SPIKE_SAMPLE:
            continue  # too little traffic to call a spike either way
        recent = float(r.failed or 0) / r.total
        baseline = method_failure_rate.get(r.method, 0.0)
        method_spike_ratio[r.method] = recent / baseline if baseline > 0.01 else (1.0 if recent < 0.05 else 3.0)

    # Historical recovery rate by observed failure reason -- the agent's memory
    # of "what has actually worked before" for this kind of failure.
    rec_rows = db.execute(
        select(
            Transaction.failure_reason,
            func.count().label("total"),
            func.sum(case((Transaction.recovered_at.is_not(None), 1), else_=0)).label("recovered"),
        )
        .where(Transaction.failure_reason.is_not(None))
        .group_by(Transaction.failure_reason)
    ).all()
    reason_recovery_rate = {
        r.failure_reason: (float(r.recovered or 0) / r.total if r.total else 0.0) for r in rec_rows
    }

    ctx = MerchantContext(
        method_failure_rate=method_failure_rate,
        method_spike_ratio=method_spike_ratio,
        reason_recovery_rate=reason_recovery_rate,
        merchant_failure_rate=merchant_failure_rate,
    )
    if use_cache:
        _cached_context = ctx
    return ctx


def invalidate_context_cache() -> None:
    global _cached_context
    _cached_context = None


def compute_features(
    txn: Transaction, customer: Customer, ctx: MerchantContext, *, now: datetime | None = None
) -> dict:
    """The structured feature object every stage reasons over."""
    now = now or _utcnow()
    failed_at = _aware(txn.failed_at) or _aware(txn.created_at) or now
    minutes_since_failure = max(0.0, (now - failed_at).total_seconds() / 60.0)

    total_payments = customer.successful_payments + customer.failed_payments
    last_payment_at = _aware(customer.last_payment_at)

    return {
        # --- transaction ---
        "transaction_id": txn.id,
        "amount_paise": txn.amount_paise,
        "amount_rupees": round(txn.amount_paise / 100, 2),
        "currency": txn.currency,
        "method": txn.method,
        "card_type": txn.card_type,
        "kind": txn.kind,
        "status": txn.status,
        "failure_reason": txn.failure_reason,
        "is_subscription": txn.kind == TxnKind.SUBSCRIPTION,
        "is_checkout_abandonment": txn.kind == TxnKind.CHECKOUT,
        # --- customer ---
        "customer_previous_successful_payments": customer.successful_payments,
        "customer_previous_failed_payments": customer.failed_payments,
        "customer_total_payments": total_payments,
        "customer_success_rate": round(customer.success_rate, 4),
        "customer_lifetime_value_paise": customer.lifetime_value_paise,
        "customer_is_new": total_payments == 0,
        "customer_opted_out": customer.opted_out,
        "customer_reminders_sent": customer.reminders_sent,
        "days_since_last_payment": (
            round((now - last_payment_at).total_seconds() / 86400, 2) if last_payment_at else None
        ),
        # --- recovery history ---
        "attempt_number": txn.retry_count + 1,
        "retry_count": txn.retry_count,
        "outreach_count": txn.outreach_count,
        "minutes_since_failure": round(minutes_since_failure, 2),
        "days_since_failure": round(minutes_since_failure / 1440, 3),
        # --- merchant / rail context ---
        "payment_method_failure_rate": round(ctx.method_failure_rate.get(txn.method, 0.0), 4),
        "merchant_failure_rate": round(ctx.merchant_failure_rate, 4),
        "recent_failure_spike_ratio": round(ctx.method_spike_ratio.get(txn.method, 1.0), 2),
        "recent_failure_spike": ctx.method_spike_ratio.get(txn.method, 1.0) >= 2.0,
        "historical_recovery_rate": round(
            ctx.reason_recovery_rate.get(txn.failure_reason or "", 0.0), 4
        ),
    }
