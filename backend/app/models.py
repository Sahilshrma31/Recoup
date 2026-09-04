"""Persistence model (design §22).

Amounts are integers in paise. `Transaction.latent` holds the synthetic
ground truth used by the outcome simulator -- the agent never reads it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .enums import AttemptStatus, RecoveryState, TxnKind, TxnStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(32))

    successful_payments: Mapped[int] = mapped_column(Integer, default=0)
    failed_payments: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_value_paise: Mapped[int] = mapped_column(Integer, default=0)
    last_payment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Set when the customer has asked not to be contacted. Hard block on outreach.
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Reminders/links already sent across all transactions in the current window.
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)

    transactions: Mapped[list[Transaction]] = relationship(back_populates="customer")

    @property
    def success_rate(self) -> float:
        total = self.successful_payments + self.failed_payments
        return self.successful_payments / total if total else 0.0


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)

    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    method: Mapped[str] = mapped_column(String(24))            # upi | card | netbanking | wallet
    card_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # credit | debit
    bank: Mapped[str | None] = mapped_column(String(32), nullable=True)

    kind: Mapped[str] = mapped_column(String(16), default=TxnKind.PAYMENT)
    status: Mapped[str] = mapped_column(String(16), default=TxnStatus.FAILED, index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovered_amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    #: Minutes from the agent's first action to the money arriving, on the
    #: *simulated* clock. Recorded at verification time because the demo
    #: compresses wall-clock time; deriving it later would report seconds.
    agent_recovery_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- recovery workflow state ---
    recovery_state: Mapped[str] = mapped_column(String(24), default=RecoveryState.DETECTED, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    outreach_count: Mapped[int] = mapped_column(Integer, default=0)
    at_risk: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Ground truth for the simulator. NEVER passed to the agent or the LLM.
    latent: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="transactions")
    attempts: Mapped[list[RecoveryAttempt]] = relationship(
        back_populates="transaction", order_by="RecoveryAttempt.attempt_number"
    )
    decisions: Mapped[list[AgentDecision]] = relationship(
        back_populates="transaction", order_by="AgentDecision.created_at"
    )

    __table_args__ = (Index("ix_txn_state_risk", "recovery_state", "at_risk"),)

    @property
    def is_recoverable_target(self) -> bool:
        return self.status != TxnStatus.CAPTURED and self.recovery_state != RecoveryState.RECOVERED


class RecoveryAttempt(Base, TimestampMixin):
    """One executed (or scheduled) recovery action."""

    __tablename__ = "recovery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("agent_decisions.id"), nullable=True)

    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    recovery_probability: Mapped[float] = mapped_column(Float, default=0.0)
    expected_recovery_paise: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(24), default=AttemptStatus.SCHEDULED, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    #: `recovery_<transaction>_<attempt_number>` -- a duplicate webhook can never
    #: produce a second financial action (design §29).
    idempotency_key: Mapped[str] = mapped_column(String(128))

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the outcome of this attempt becomes knowable (a retry resolves in
    #: seconds, a payment link may take hours).
    resolve_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    provider_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_attempts: Mapped[int] = mapped_column(Integer, default=0)

    transaction: Mapped[Transaction] = relationship(back_populates="attempts")

    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_attempt_idempotency"),)


class AgentDecision(Base, TimestampMixin):
    """Immutable audit record: everything behind one "Why did AI do this?" click."""

    __tablename__ = "agent_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), index=True)

    diagnosis: Mapped[str] = mapped_column(String(64))          # cause code
    category: Mapped[str] = mapped_column(String(40))           # DiagnosisCategory
    diagnosis_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    recommended_action: Mapped[str] = mapped_column(String(32))  # what the AI proposed
    action: Mapped[str] = mapped_column(String(32))              # what policy allowed
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0)

    recovery_probability: Mapped[float] = mapped_column(Float, default=0.0)
    expected_recovery_paise: Mapped[int] = mapped_column(Integer, default=0)

    reasoning_summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20))              # DecisionSource
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    features: Mapped[dict] = mapped_column(JSON, default=dict)
    action_scores: Mapped[list] = mapped_column(JSON, default=list)
    policy_result: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # sanitised prompt input

    transaction: Mapped[Transaction] = relationship(back_populates="decisions")


class ActivityEvent(Base, TimestampMixin):
    """Rows behind the live AI activity feed (design §20)."""

    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(24))   # detect|diagnose|predict|decide|guard|act|verify
    level: Mapped[str] = mapped_column(String(12), default="info")
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ExperimentResult(Base, TimestampMixin):
    """Stored A/B run (design §28) so the dashboard shows measured, not claimed, numbers."""

    __tablename__ = "experiment_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), default="baseline_vs_agent")
    payload: Mapped[dict] = mapped_column(JSON)
