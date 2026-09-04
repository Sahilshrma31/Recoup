"""API response shapes.

Amounts cross the wire in **both** paise (integer, authoritative) and rupees
(float, for display). The frontend never does money arithmetic; it formats.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def rupees(paise: int | None) -> float:
    return round((paise or 0) / 100, 2)


def _stamp_utc(value: datetime | None) -> datetime | None:
    """Mark naive datetimes as UTC.

    Everything is stored in UTC, but SQLite hands back naive datetimes and a
    naive ISO string is interpreted by browsers as *local* time -- which
    silently shifted every timestamp in the UI by the viewer's offset. Stamping
    the zone here fixes it once, at the boundary, for every response.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


#: Use in place of `datetime` on every response model.
UtcDatetime = Annotated[datetime, AfterValidator(_stamp_utc)]


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str
    phone: str
    successful_payments: int
    failed_payments: int
    lifetime_value_paise: int
    opted_out: bool
    last_payment_at: UtcDatetime | None = None


class TransactionSummary(BaseModel):
    id: str
    customer_id: str
    customer_name: str
    amount_paise: int
    amount_rupees: float
    currency: str
    method: str
    kind: str
    status: str
    failure_reason: str | None
    recovery_state: str
    at_risk: bool
    retry_count: int
    outreach_count: int
    failed_at: UtcDatetime | None
    recovered_at: UtcDatetime | None
    recovered_amount_paise: int
    # Latest decision, when the agent has already looked at this transaction.
    recommended_action: str | None = None
    recovery_probability: float | None = None
    expected_recovery_paise: int | None = None
    diagnosis: str | None = None


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transaction_id: str
    created_at: UtcDatetime
    diagnosis: str
    category: str
    diagnosis_confidence: float
    recommended_action: str
    action: str
    delay_minutes: int
    recovery_probability: float
    expected_recovery_paise: int
    reasoning_summary: str
    source: str
    model: str | None
    latency_ms: int
    action_scores: list[dict]
    policy_result: dict
    features: dict


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    status: str
    attempt_number: int
    idempotency_key: str
    recovery_probability: float
    expected_recovery_paise: int
    provider_ref: str | None
    provider_url: str | None
    error: str | None
    scheduled_for: UtcDatetime | None
    executed_at: UtcDatetime | None
    completed_at: UtcDatetime | None


class TransactionDetail(TransactionSummary):
    customer: CustomerOut
    bank: str | None = None
    card_type: str | None = None
    subscription_id: str | None = None
    stop_reason: str | None = None
    decisions: list[DecisionOut] = Field(default_factory=list)
    attempts: list[AttemptOut] = Field(default_factory=list)
    activity: list[dict] = Field(default_factory=list)


class OverviewOut(BaseModel):
    revenue_at_risk_paise: int
    estimated_recoverable_paise: int
    revenue_recovered_paise: int
    recovery_rate: float
    at_risk_transactions: int
    recovered_transactions: int
    pending_analysis: int


class PipelineItem(BaseModel):
    action: str
    count: int
    value_paise: int


class PerformanceOut(BaseModel):
    attempts_completed: int
    action_precision: float
    retries_executed: int
    false_retry_rate: float
    avg_recovery_minutes: float
    avg_agent_recovery_minutes: float
    manual_intervention_rate: float
    deliberately_stopped: int


class ActionResponse(BaseModel):
    transaction_id: str
    action: str
    recovery_state: str
    decision_id: int | None = None
    attempt_id: int | None = None
    message: str
