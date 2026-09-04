"""Recoup's read-only MCP server.

Why this exists, and why it is read-only
----------------------------------------
Razorpay ships an MCP server so a model can *call* payment tools. Recoup
deliberately does not give a model that ability: the whole architecture rests
on the model reasoning while deterministic code moves the money, behind a
policy engine the model cannot reach. Wiring an agent's tool loop into the
execution path would hand back exactly the capability the design took away.

So MCP is used for the other half of the problem -- a human asking questions.
A merchant ops person in Claude Desktop can ask "what happened to pay_92831?"
or "how much did we recover this week?" and get a grounded answer out of the
same tables the dashboard reads. Every tool here is a SELECT. There is no
recover, no retry, no approve: acting stays behind the API's policy checks and
its audit trail.

Run it:      python -m app.mcp_server
Claude:      see the `mcp` block in README.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal, init_db
from .enums import Action, TxnStatus
from .models import AgentDecision, RecoveryAttempt, Transaction
from .services import analytics_service

log = logging.getLogger("recoup.mcp")

#: Every tool on this server only reads. Advertised so a client can show it.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

server = MCPServer(
    name="recoup",
    version="1.0.0",
    instructions=(
        "Recoup is an autonomous revenue recovery agent for Razorpay merchants. "
        "It diagnoses failed payments, predicts recovery probability, and acts within "
        "deterministic guardrails. These tools are READ-ONLY: they explain what the agent "
        "decided and why, and report recovery performance. They cannot retry a payment, "
        "create a payment link, or approve an action -- those stay behind the agent's own "
        "policy engine. Amounts are returned in rupees. When a user asks why a decision was "
        "made, prefer explain_decision, which returns the scorecard and the guardrail results."
    ),
)


def _rupees(paise: int | None) -> float:
    return round((paise or 0) / 100, 2)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


@server.tool(
    name="get_transaction",
    description=(
        "Look up one transaction by id (e.g. 'pay_92831'): amount, method, failure reason, "
        "customer payment history, recovery state, and every recovery action attempted."
    ),
    annotations=READ_ONLY,
)
def get_transaction(transaction_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        txn = db.get(Transaction, transaction_id)
        if txn is None:
            return {"found": False, "transaction_id": transaction_id}

        customer = txn.customer
        return {
            "found": True,
            "transaction_id": txn.id,
            "amount_rupees": _rupees(txn.amount_paise),
            "currency": txn.currency,
            "method": txn.method,
            "card_type": txn.card_type,
            "bank": txn.bank,
            "kind": txn.kind,
            "status": txn.status,
            "failure_reason": txn.failure_reason,
            "failed_at": _iso(txn.failed_at),
            "recovery_state": txn.recovery_state,
            "at_risk": txn.at_risk,
            "recovered": txn.recovered_at is not None,
            "recovered_at": _iso(txn.recovered_at),
            "recovered_rupees": _rupees(txn.recovered_amount_paise),
            "agent_recovery_minutes": txn.agent_recovery_minutes,
            "stop_reason": txn.stop_reason,
            "retry_count": txn.retry_count,
            "outreach_count": txn.outreach_count,
            "customer": {
                # Deliberately no email or phone: this is an operations view,
                # and the name plus history is enough to reason about a case.
                "id": customer.id,
                "name": customer.name,
                "successful_payments": customer.successful_payments,
                "failed_payments": customer.failed_payments,
                "success_rate": round(customer.success_rate, 3),
                "lifetime_value_rupees": _rupees(customer.lifetime_value_paise),
                "opted_out": customer.opted_out,
            },
            "attempts": [
                {
                    "action": a.action,
                    "status": a.status,
                    "attempt_number": a.attempt_number,
                    "executed_at": _iso(a.executed_at),
                    "completed_at": _iso(a.completed_at),
                    "error": a.error,
                }
                for a in txn.attempts
            ],
        }


@server.tool(
    name="explain_decision",
    description=(
        "Explain WHY the agent chose what it chose for a transaction: the diagnosis and its "
        "confidence, the scored alternatives with expected values, which guardrails passed or "
        "blocked, and whether the policy engine overruled the agent's first choice. "
        "Use this for any 'why did it do that' question."
    ),
    annotations=READ_ONLY,
)
def explain_decision(transaction_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        decision = db.execute(
            select(AgentDecision)
            .where(AgentDecision.transaction_id == transaction_id)
            .order_by(AgentDecision.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if decision is None:
            return {
                "found": False,
                "transaction_id": transaction_id,
                "note": "No decision recorded. The agent has not analysed this transaction yet.",
            }

        policy = decision.policy_result or {}
        overruled = decision.recommended_action != decision.action
        return {
            "found": True,
            "transaction_id": transaction_id,
            "decided_at": _iso(decision.created_at),
            "reasoning": decision.reasoning_summary,
            "diagnosis": {
                "cause": decision.diagnosis,
                "category": decision.category,
                "confidence": decision.diagnosis_confidence,
            },
            "action_taken": decision.action,
            "delay_minutes": decision.delay_minutes,
            "recovery_probability": round(decision.recovery_probability, 3),
            "expected_recovery_rupees": _rupees(decision.expected_recovery_paise),
            "reasoned_by": (
                f"{decision.model} (LLM)" if decision.source == "llm"
                else "deterministic rules engine"
            ),
            "policy_override": {
                "occurred": overruled,
                "agent_first_choice": decision.recommended_action if overruled else None,
                "reason": policy.get("override_reason") if overruled else None,
            },
            "requires_merchant_approval": bool(policy.get("requires_approval")),
            "alternatives_considered": [
                {
                    "action": s.get("action"),
                    "recovery_probability": round(s.get("probability") or 0.0, 3),
                    "expected_recovery_rupees": _rupees(s.get("expected_value_paise")),
                    "net_expected_rupees": _rupees(s.get("net_expected_value_paise")),
                }
                for s in (decision.action_scores or [])
            ],
            "guardrails": [
                {"check": c.get("name"), "result": c.get("status"), "detail": c.get("detail")}
                for c in policy.get("checks", [])
            ],
        }


@server.tool(
    name="recovery_metrics",
    description=(
        "Headline recovery performance: revenue at risk, revenue recovered, recovery rate, "
        "action precision, false retry rate, average recovery time, and how many transactions "
        "the agent deliberately stopped working on."
    ),
    annotations=READ_ONLY,
)
def recovery_metrics() -> dict[str, Any]:
    with SessionLocal() as db:
        overview = analytics_service.overview(db)
        performance = analytics_service.performance(db)
        return {
            "revenue_at_risk_rupees": _rupees(overview["revenue_at_risk_paise"]),
            "revenue_recovered_rupees": _rupees(overview["revenue_recovered_paise"]),
            "estimated_recoverable_rupees": _rupees(overview["estimated_recoverable_paise"]),
            "recovery_rate": overview["recovery_rate"],
            "open_transactions": overview["at_risk_transactions"],
            "recovered_transactions": overview["recovered_transactions"],
            "awaiting_analysis": overview["pending_analysis"],
            "action_precision": performance["action_precision"],
            "false_retry_rate": performance["false_retry_rate"],
            "avg_agent_recovery_minutes": performance["avg_agent_recovery_minutes"],
            "deliberately_stopped": performance["deliberately_stopped"],
            "execution_mode": "live-razorpay" if settings.live_execution else "simulated",
        }


@server.tool(
    name="list_at_risk",
    description=(
        "List open at-risk transactions, highest value first. Optionally filter by the "
        "recommended action (RETRY, RETRY_DELAYED, CREATE_PAYMENT_LINK, SEND_REMINDER, "
        "RETRY_SUBSCRIPTION, NO_ACTION), by payment method, or by a minimum amount in rupees."
    ),
    annotations=READ_ONLY,
)
def list_at_risk(
    limit: int = 20,
    action: str | None = None,
    method: str | None = None,
    min_amount_rupees: float = 0,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    with SessionLocal() as db:
        latest = (
            select(
                AgentDecision.transaction_id.label("txn_id"),
                func.max(AgentDecision.created_at).label("at"),
            )
            .group_by(AgentDecision.transaction_id)
            .subquery()
        )
        query = (
            select(Transaction, AgentDecision)
            .outerjoin(latest, latest.c.txn_id == Transaction.id)
            .outerjoin(
                AgentDecision,
                (AgentDecision.transaction_id == Transaction.id)
                & (AgentDecision.created_at == latest.c.at),
            )
            .where(
                Transaction.at_risk.is_(True),
                Transaction.status != TxnStatus.CAPTURED,
                Transaction.amount_paise >= int(min_amount_rupees * 100),
            )
            .order_by(Transaction.amount_paise.desc())
            .limit(limit)
        )
        if action:
            query = query.where(AgentDecision.action == action.upper())
        if method:
            query = query.where(Transaction.method == method.lower())

        rows = db.execute(query).all()
        return {
            "count": len(rows),
            "filters": {
                "action": action, "method": method, "min_amount_rupees": min_amount_rupees,
            },
            "transactions": [
                {
                    "transaction_id": txn.id,
                    "amount_rupees": _rupees(txn.amount_paise),
                    "method": txn.method,
                    "failure_reason": txn.failure_reason,
                    "customer_name": txn.customer.name,
                    "recovery_state": txn.recovery_state,
                    "recommended_action": decision.action if decision else None,
                    "recovery_probability": (
                        round(decision.recovery_probability, 3) if decision else None
                    ),
                    "expected_recovery_rupees": (
                        _rupees(decision.expected_recovery_paise) if decision else None
                    ),
                }
                for txn, decision in rows
            ],
        }


@server.tool(
    name="failure_breakdown",
    description=(
        "Group recent failures by reason or by payment method, with counts, value at risk, and "
        "how much has been recovered for each. Use this to answer 'what is failing and why'."
    ),
    annotations=READ_ONLY,
)
def failure_breakdown(group_by: str = "reason", days: int = 30) -> dict[str, Any]:
    column = Transaction.method if group_by == "method" else Transaction.failure_reason
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))

    with SessionLocal() as db:
        rows = db.execute(
            select(
                column.label("key"),
                func.count().label("count"),
                func.sum(Transaction.amount_paise).label("at_risk"),
                func.sum(Transaction.recovered_amount_paise).label("recovered"),
            )
            .where(Transaction.status != TxnStatus.CAPTURED, Transaction.created_at >= since)
            .group_by(column)
            .order_by(func.sum(Transaction.amount_paise).desc())
        ).all()

        return {
            "grouped_by": "method" if group_by == "method" else "failure_reason",
            "window_days": days,
            "groups": [
                {
                    "key": r.key or "unknown",
                    "transactions": r.count,
                    "at_risk_rupees": _rupees(r.at_risk),
                    "recovered_rupees": _rupees(r.recovered),
                }
                for r in rows
            ],
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    log.info("Recoup MCP server ready (read-only, %d tools)", 5)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
