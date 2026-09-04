"""Measured A/B: fixed retry policy vs. the recovery agent (design §28).

What this does
--------------
Replays the same at-risk transactions through two strategies and samples
outcomes from the hidden ground truth in `services/simulator.py`.

Three choices keep the comparison honest:

1. **Paired.** Both strategies see the same transaction with the same RNG
   stream, seeded from the transaction id. Differences come from the policy,
   not from luck.
2. **The agent gets no privileged information.** It sees the same noisy failure
   code a merchant sees. The latent cause is read only by the outcome sampler.
3. **The counterfactual is stated.** By default every transaction is replayed
   as if it had just failed ("what if the agent had been running when these
   came in"), because comparing recovery policies on a backlog that is already
   three weeks stale mostly measures the staleness. `--use-actual-age` runs it
   the other way.

The agent arm here is the *deterministic* agent -- diagnosis, scorecard and
policy engine, no model calls -- because this is a few thousand decisions.
That makes the result a floor, not a ceiling: the LLM layer's contribution is
ambiguity resolution on top of this.

Usage:  python -m scripts.experiment [--limit 2000] [--use-actual-age] [--seed 7]
"""
from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.agent.diagnosis import diagnose  # noqa: E402
from app.agent.features import MerchantContext, build_merchant_context, compute_features  # noqa: E402
from app.agent.planner import plan as build_plan  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.enums import Action, RecoveryState, TxnStatus  # noqa: E402
from app.models import ExperimentResult, Transaction  # noqa: E402
from app.policy.guardrails import PolicyEngine  # noqa: E402
from app.services import simulator  # noqa: E402
from app.services.simulator import Latent  # noqa: E402

#: A retry whose true success probability is below this was never going to work.
#: Ground truth is used here for *scoring* the experiment, never for deciding.
FUTILE_RETRY_THRESHOLD = 0.05

#: How long each action takes to resolve, in simulated minutes.
_STEP_MINUTES = {
    Action.RETRY: 2, Action.RETRY_DELAYED: 0, Action.RETRY_SUBSCRIPTION: 3,
    Action.CREATE_PAYMENT_LINK: 45, Action.SEND_REMINDER: 120, Action.ESCALATE: 240,
}
MAX_STEPS = 4


@dataclass
class ArmResult:
    name: str
    transactions: int = 0
    recovered: int = 0
    recovered_paise: int = 0
    exposure_paise: int = 0
    attempts: int = 0
    retries: int = 0
    futile_retries: int = 0
    outreach: int = 0
    contacted_customers: int = 0
    stopped_early: int = 0
    recovery_minutes: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "transactions": self.transactions,
            "recovered_transactions": self.recovered,
            "recovery_rate": round(self.recovered / self.transactions, 4) if self.transactions else 0.0,
            "revenue_recovered_paise": self.recovered_paise,
            "revenue_at_risk_paise": self.exposure_paise,
            "value_recovery_rate": (
                round(self.recovered_paise / self.exposure_paise, 4) if self.exposure_paise else 0.0
            ),
            "actions_executed": self.attempts,
            "retries_executed": self.retries,
            "futile_retry_rate": round(self.futile_retries / self.retries, 4) if self.retries else 0.0,
            "messages_sent": self.outreach,
            "customer_contact_rate": (
                round(self.contacted_customers / self.transactions, 4) if self.transactions else 0.0
            ),
            "deliberately_stopped": self.stopped_early,
            "avg_recovery_minutes": (
                round(sum(self.recovery_minutes) / len(self.recovery_minutes), 1)
                if self.recovery_minutes else 0.0
            ),
        }


def _sample(action: Action, latent: Latent, rng: random.Random, *, attempt_no: int,
            outreach_no: int, minutes_waited: float, days: float) -> tuple[bool, float]:
    p = simulator.success_probability(
        action, latent, attempt_number=attempt_no, outreach_number=outreach_no,
        minutes_waited=minutes_waited, days_since_failure=days,
    )
    return rng.random() < p, p


def run_baseline(features: dict, latent: Latent, rng: random.Random, start_age_minutes: float) -> dict:
    """Today's default: retry once, then send a generic reminder. Never stop.

    This is not a straw man -- it is what most merchants actually do, and it
    recovers real money. The agent has to beat it, not just differ from it.
    """
    elapsed, retries, outreach, actions, futile = 0.0, 0, 0, 0, 0
    for action in (Action.RETRY, Action.SEND_REMINDER):
        elapsed += _STEP_MINUTES[action]
        total_minutes = start_age_minutes + elapsed
        actions += 1
        if action is Action.RETRY:
            retries += 1
        else:
            outreach += 1
        ok, p = _sample(
            action, latent, rng,
            attempt_no=retries, outreach_no=max(0, outreach - 1),
            minutes_waited=total_minutes, days=total_minutes / 1440,
        )
        if action is Action.RETRY and p < FUTILE_RETRY_THRESHOLD:
            futile += 1
        if ok:
            return {"recovered": True, "minutes": elapsed, "actions": actions,
                    "retries": retries, "outreach": outreach, "futile": futile, "stopped": False}
    return {"recovered": False, "minutes": elapsed, "actions": actions,
            "retries": retries, "outreach": outreach, "futile": futile, "stopped": False}


def run_agent(txn: Transaction, features: dict, latent: Latent, rng: random.Random,
              start_age_minutes: float, engine: PolicyEngine) -> dict:
    """Diagnose, score, guard, act -- and stop when it stops being worth it."""
    state = dict(features)
    elapsed, retries, outreach, actions, futile = 0.0, 0, 0, 0, 0

    for _ in range(MAX_STEPS):
        state["retry_count"] = retries
        state["outreach_count"] = outreach
        state["attempt_number"] = retries + 1
        total_minutes = start_age_minutes + elapsed
        state["minutes_since_failure"] = total_minutes
        state["days_since_failure"] = total_minutes / 1440

        diagnosis = diagnose(state)
        plan = build_plan(
            state, diagnosis,
            settings=settings,
            recovery_state=RecoveryState.ANALYZING,
            minutes_since_last_attempt=(elapsed if actions else None),
            merchant_approved=True,   # the experiment measures policy, not the approval UI
            policy_engine=engine,
        )
        action = plan.action
        if action is Action.NO_ACTION:
            return {"recovered": False, "minutes": elapsed, "actions": actions, "retries": retries,
                    "outreach": outreach, "futile": futile, "stopped": True}

        # A delayed retry spends its cooldown before the attempt lands.
        elapsed += plan.delay_minutes + _STEP_MINUTES.get(action, 5)
        total_minutes = start_age_minutes + elapsed
        actions += 1
        if action in (Action.RETRY, Action.RETRY_DELAYED, Action.RETRY_SUBSCRIPTION):
            retries += 1
        if action in (Action.CREATE_PAYMENT_LINK, Action.SEND_REMINDER):
            outreach += 1

        ok, p = _sample(
            action, latent, rng,
            attempt_no=retries, outreach_no=max(0, outreach - 1),
            minutes_waited=total_minutes, days=total_minutes / 1440,
        )
        if action in (Action.RETRY, Action.RETRY_DELAYED, Action.RETRY_SUBSCRIPTION) and p < FUTILE_RETRY_THRESHOLD:
            futile += 1
        if ok:
            return {"recovered": True, "minutes": elapsed, "actions": actions, "retries": retries,
                    "outreach": outreach, "futile": futile, "stopped": False}

    return {"recovered": False, "minutes": elapsed, "actions": actions, "retries": retries,
            "outreach": outreach, "futile": futile, "stopped": False}


def _accumulate(arm: ArmResult, txn: Transaction, outcome: dict) -> None:
    arm.transactions += 1
    arm.exposure_paise += txn.amount_paise
    arm.attempts += outcome["actions"]
    arm.retries += outcome["retries"]
    arm.futile_retries += outcome["futile"]
    arm.outreach += outcome["outreach"]
    if outcome["outreach"]:
        arm.contacted_customers += 1
    if outcome["stopped"]:
        arm.stopped_early += 1
    if outcome["recovered"]:
        arm.recovered += 1
        arm.recovered_paise += txn.amount_paise
        arm.recovery_minutes.append(outcome["minutes"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Baseline vs agent recovery experiment.")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--use-actual-age", action="store_true",
                        help="replay with each transaction's real age instead of as-if-fresh")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        txns = db.execute(
            select(Transaction)
            .options(selectinload(Transaction.customer))
            .where(Transaction.status != TxnStatus.CAPTURED, Transaction.latent.is_not(None))
            .order_by(Transaction.amount_paise.desc())
            .limit(args.limit)
        ).scalars().all()
        if not txns:
            print("No at-risk transactions with ground truth. Run scripts/seed.py first.")
            return

        ctx = build_merchant_context(db, use_cache=False)
        engine = PolicyEngine(settings)
        now = datetime.now(timezone.utc)

        baseline, agent = ArmResult("baseline_retry_and_remind"), ArmResult("recovery_agent")

        for txn in txns:
            latent = Latent.from_dict(txn.latent)
            if latent is None:
                continue
            features = compute_features(txn, txn.customer, ctx, now=now)

            if args.use_actual_age:
                start_age = float(features["minutes_since_failure"])
            else:
                start_age = 0.0
                features = dict(features, minutes_since_failure=0.0, days_since_failure=0.0)

            # Paired: identical RNG stream per transaction for both arms.
            seed = f"{args.seed}:{txn.id}"
            _accumulate(baseline, txn, run_baseline(features, latent, random.Random(seed), start_age))
            _accumulate(agent, txn, run_agent(txn, features, latent, random.Random(seed), start_age, engine))

        payload = {
            "transactions": len(txns),
            "counterfactual": "actual_age" if args.use_actual_age else "as_if_freshly_failed",
            "agent_arm": "deterministic (diagnosis + scorecard + policy engine, no model calls)",
            "seed": args.seed,
            "arms": {"baseline": baseline.to_dict(), "agent": agent.to_dict()},
        }
        b, a = payload["arms"]["baseline"], payload["arms"]["agent"]
        payload["deltas"] = {
            "recovery_rate_pp": round((a["recovery_rate"] - b["recovery_rate"]) * 100, 2),
            "revenue_recovered_paise": a["revenue_recovered_paise"] - b["revenue_recovered_paise"],
            "revenue_uplift_pct": (
                round((a["revenue_recovered_paise"] / b["revenue_recovered_paise"] - 1) * 100, 1)
                if b["revenue_recovered_paise"] else None
            ),
            "futile_retry_rate_pp": round((a["futile_retry_rate"] - b["futile_retry_rate"]) * 100, 2),
            "customer_contact_rate_pp": round(
                (a["customer_contact_rate"] - b["customer_contact_rate"]) * 100, 2
            ),
            "messages_saved": b["messages_sent"] - a["messages_sent"],
        }

        _print(payload)

        if not args.no_save:
            db.add(ExperimentResult(label="baseline_vs_agent", payload=payload))
            db.commit()
            print("\nSaved to experiment_results (served at GET /api/analytics/experiment).")
    finally:
        db.close()


def _print(payload: dict) -> None:
    b, a, d = payload["arms"]["baseline"], payload["arms"]["agent"], payload["deltas"]
    rs = lambda p: f"Rs {p / 100:,.0f}"
    print(f"\n{'=' * 74}")
    print(f"  BASELINE vs RECOVERY AGENT  --  {payload['transactions']:,} transactions")
    print(f"  counterfactual: {payload['counterfactual']}   agent arm: {payload['agent_arm']}")
    print("=" * 74)
    rows = [
        ("Recovery rate", f"{b['recovery_rate']:.1%}", f"{a['recovery_rate']:.1%}", f"{d['recovery_rate_pp']:+.1f} pp"),
        ("Revenue recovered", rs(b["revenue_recovered_paise"]), rs(a["revenue_recovered_paise"]),
         f"{d['revenue_uplift_pct']:+.1f}%" if d["revenue_uplift_pct"] is not None else "-"),
        ("Value recovery rate", f"{b['value_recovery_rate']:.1%}", f"{a['value_recovery_rate']:.1%}", ""),
        ("Actions executed", f"{b['actions_executed']:,}", f"{a['actions_executed']:,}", ""),
        ("Retries executed", f"{b['retries_executed']:,}", f"{a['retries_executed']:,}", ""),
        ("Futile retry rate", f"{b['futile_retry_rate']:.1%}", f"{a['futile_retry_rate']:.1%}",
         f"{d['futile_retry_rate_pp']:+.1f} pp"),
        ("Customers contacted", f"{b['customer_contact_rate']:.1%}", f"{a['customer_contact_rate']:.1%}",
         f"{d['customer_contact_rate_pp']:+.1f} pp"),
        ("Messages sent", f"{b['messages_sent']:,}", f"{a['messages_sent']:,}", f"{-d['messages_saved']:+,}"),
        ("Deliberately stopped", f"{b['deliberately_stopped']:,}", f"{a['deliberately_stopped']:,}", ""),
        ("Avg recovery time", f"{b['avg_recovery_minutes']:.0f} min", f"{a['avg_recovery_minutes']:.0f} min", ""),
    ]
    print(f"  {'':24} {'Baseline':>14} {'AI Agent':>14} {'Delta':>12}")
    print(f"  {'-' * 68}")
    for label, bv, av, dv in rows:
        print(f"  {label:24} {bv:>14} {av:>14} {dv:>12}")
    print("=" * 74)


if __name__ == "__main__":
    main()
