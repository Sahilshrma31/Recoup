"""Synthetic dataset generator (design §26).

Produces a merchant's recent payment history with three properties the agent
needs in order to be interesting:

1. **Customers with real histories.** Recovery decisions are only meaningful if
   "this customer has paid 14 times before" is actually true in the data.
2. **Hidden ground truth.** Every failure gets a latent cause and a customer
   willingness-to-pay, which the outcome simulator reads and the agent cannot.
   The agent only sees a *noisy emission* of that cause.
3. **A live rail-degradation window.** A burst of UPI failures in the last few
   minutes, so the "is this the bank or the customer?" inference has something
   real to detect.

Usage:  python -m scripts.seed [--transactions 10000] [--reset]
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.enums import RecoveryState, TxnKind, TxnStatus  # noqa: E402
from app.models import (  # noqa: E402
    ActivityEvent, AgentDecision, Customer, ExperimentResult, RecoveryAttempt, Transaction,
)
from app.services.simulator import Latent, observe_failure_reason  # noqa: E402

FIRST_NAMES = [
    "Rahul", "Priya", "Arjun", "Ananya", "Vikram", "Sneha", "Karthik", "Meera", "Rohan", "Divya",
    "Aditya", "Kavya", "Siddharth", "Ishita", "Nikhil", "Aarti", "Manish", "Pooja", "Varun", "Neha",
    "Sanjay", "Ritu", "Akash", "Tanvi", "Gaurav", "Shreya", "Harsh", "Lakshmi", "Imran", "Farah",
]
LAST_NAMES = [
    "Sharma", "Iyer", "Patel", "Reddy", "Nair", "Gupta", "Menon", "Desai", "Rao", "Kulkarni",
    "Banerjee", "Chopra", "Joshi", "Mehta", "Pillai", "Khan", "Verma", "Bose", "Shah", "Naidu",
]
BANKS = ["HDFC", "ICICI", "SBI", "Axis", "Kotak", "IndusInd", "Yes", "PNB"]
METHODS = ["upi", "card", "netbanking", "wallet"]
METHOD_WEIGHTS = [0.58, 0.28, 0.09, 0.05]

#: Latent causes for one-off payment failures, and how common each is.
PAYMENT_CAUSES = [
    ("temp_rail", 0.22), ("temp_bank", 0.16), ("funds_transient", 0.15),
    ("funds_persistent", 0.10), ("dead_instrument", 0.14), ("risk_block", 0.07),
    ("velocity_block", 0.05), ("customer_distracted", 0.07), ("customer_disengaged", 0.04),
]
CHECKOUT_CAUSES = [("customer_distracted", 0.62), ("customer_disengaged", 0.38)]
SUBSCRIPTION_CAUSES = [("mandate_transient", 0.58), ("mandate_dead", 0.42)]

#: Failures newer than this are left untouched for the live agent to work on.
LIVE_WINDOW_MINUTES = 90


def _pick(options: list[tuple[str, float]], rng: random.Random) -> str:
    names, weights = zip(*options)
    return rng.choices(names, weights=weights, k=1)[0]


def _amount_paise(rng: random.Random) -> int:
    """Long-tailed ticket sizes: mostly small, occasionally large."""
    rupees = min(49_999, max(199, int(rng.lognormvariate(7.4, 0.85))))
    return rupees * 100


def _reliability(rng: random.Random) -> float:
    """A customer's intrinsic tendency to pay successfully."""
    roll = rng.random()
    if roll < 0.55:
        return rng.uniform(0.85, 0.99)   # loyal, reliable
    if roll < 0.85:
        return rng.uniform(0.55, 0.85)   # ordinary
    return rng.uniform(0.10, 0.55)       # chronically problematic


def build_customers(rng: random.Random, count: int, now: datetime) -> list[Customer]:
    customers = []
    for i in range(count):
        rel = _reliability(rng)
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        handle = name.lower().replace(" ", ".")
        customers.append(
            Customer(
                id=f"cust_{i + 1000}",
                name=name,
                email=f"{handle}{i}@example.com",
                phone=f"+9198{rng.randint(10_000_000, 99_999_999)}",
                successful_payments=0,
                failed_payments=0,
                lifetime_value_paise=0,
                last_payment_at=None,
                opted_out=rng.random() < 0.04,
                reminders_sent=0,
                created_at=now - timedelta(days=rng.randint(30, 720)),
            )
        )
    # Stash the hidden reliability on the object for the generator's own use.
    for c, rel in zip(customers, (_reliability(rng) for _ in customers)):
        c._reliability = rel  # type: ignore[attr-defined]
    return customers


def generate(
    *, total: int, seed: int, reset: bool, demo: bool
) -> dict:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)

    init_db()
    db = SessionLocal()
    try:
        if reset:
            for model in (ActivityEvent, RecoveryAttempt, AgentDecision, ExperimentResult,
                          Transaction, Customer):
                db.query(model).delete()
            db.commit()

        n_success = int(total * 0.75)
        n_failed = int(total * 0.15)
        n_checkout = int(total * 0.07)
        n_subscription = total - n_success - n_failed - n_checkout

        customer_count = max(50, total // 8)
        customers = build_customers(rng, customer_count, now)
        db.add_all(customers)
        db.flush()

        txn_rows: list[dict] = []
        counters = {c.id: [0, 0, 0] for c in customers}  # ok, failed, ltv

        def add_txn(**kw) -> None:
            txn_rows.append(kw)

        # --- successful payments (the behavioural history) ----------------
        for i in range(n_success):
            c = rng.choices(customers, weights=[getattr(x, "_reliability", 0.5) for x in customers])[0]
            created = now - timedelta(minutes=rng.randint(30, 60 * 24 * 30))
            amount = _amount_paise(rng)
            method = rng.choices(METHODS, weights=METHOD_WEIGHTS)[0]
            add_txn(
                id=f"pay_{100000 + i}", razorpay_payment_id=f"pay_{100000 + i}",
                razorpay_order_id=f"order_{100000 + i}", customer_id=c.id,
                amount_paise=amount, currency="INR", method=method,
                card_type=(rng.choice(["credit", "debit"]) if method == "card" else None),
                bank=rng.choice(BANKS), kind=TxnKind.PAYMENT, status=TxnStatus.CAPTURED,
                failure_reason=None, created_at=created, failed_at=None,
                recovered_at=None, recovered_amount_paise=0,
                recovery_state=RecoveryState.RECOVERED, at_risk=False, latent=None,
                retry_count=0, outreach_count=0,
            )
            counters[c.id][0] += 1
            counters[c.id][2] += amount

        # --- failures, checkouts and subscription renewals -----------------
        idx = n_success
        specs = (
            [(TxnKind.PAYMENT, PAYMENT_CAUSES)] * n_failed
            + [(TxnKind.CHECKOUT, CHECKOUT_CAUSES)] * n_checkout
            + [(TxnKind.SUBSCRIPTION, SUBSCRIPTION_CAUSES)] * n_subscription
        )
        rng.shuffle(specs)

        for offset, (kind, causes) in enumerate(specs):
            c = rng.choice(customers)
            rel = getattr(c, "_reliability", 0.5)
            cause = _pick(causes, rng)
            # Willingness correlates with reliability but is not identical --
            # a good payer can still lose interest in a particular purchase.
            willingness = max(0.02, min(0.98, rng.gauss(rel, 0.18)))
            latent = Latent(
                true_cause=cause,
                willingness=round(willingness, 3),
                rail_recovery_minutes=(
                    rng.randint(4, 45) if cause in {"temp_rail", "temp_bank", "velocity_block",
                                                    "mandate_transient"} else 0
                ),
            )
            minutes_ago = rng.randint(LIVE_WINDOW_MINUTES + 5, 60 * 24 * 21)
            created = now - timedelta(minutes=minutes_ago)
            amount = _amount_paise(rng)
            method = "card" if kind == TxnKind.SUBSCRIPTION else rng.choices(METHODS, weights=METHOD_WEIGHTS)[0]
            observed = (
                "checkout_abandoned" if kind == TxnKind.CHECKOUT
                else observe_failure_reason(cause, rng)
            )
            i = idx + offset
            add_txn(
                id=f"pay_{100000 + i}",
                razorpay_payment_id=(None if kind == TxnKind.CHECKOUT else f"pay_{100000 + i}"),
                razorpay_order_id=f"order_{100000 + i}", customer_id=c.id,
                amount_paise=amount, currency="INR", method=method,
                card_type=(rng.choice(["credit", "debit"]) if method == "card" else None),
                bank=rng.choice(BANKS), kind=kind,
                status=(TxnStatus.CREATED if kind == TxnKind.CHECKOUT else TxnStatus.FAILED),
                failure_reason=observed, created_at=created, failed_at=created,
                subscription_id=(f"sub_{rng.randint(1000, 9999)}" if kind == TxnKind.SUBSCRIPTION else None),
                recovered_at=None, recovered_amount_paise=0,
                recovery_state=RecoveryState.DETECTED, at_risk=True,
                latent=latent.to_dict(), retry_count=0, outreach_count=0,
            )
            counters[c.id][1] += 1

        db.bulk_insert_mappings(Transaction, txn_rows)

        # --- customer aggregates ------------------------------------------
        for c in customers:
            ok, failed, ltv = counters[c.id]
            c.successful_payments = ok
            c.failed_payments = failed
            c.lifetime_value_paise = ltv
            if ok:
                c.last_payment_at = now - timedelta(days=rng.randint(1, 40))
        db.commit()

        extra = {}
        if demo:
            extra = seed_demo_scenarios(db, rng, now, customers)
        db.commit()

        return {
            "customers": len(customers),
            "successful": n_success,
            "failed": n_failed,
            "checkout_abandoned": n_checkout,
            "subscription_failures": n_subscription,
            **extra,
        }
    finally:
        db.close()


def seed_demo_scenarios(db, rng: random.Random, now: datetime, customers: list[Customer]) -> dict:
    """The three scenarios from the design doc, plus a live UPI outage burst.

    These are pinned so a demo is reproducible; everything else is random.
    """
    # A named, reliable customer for the hero scenario.
    hero = Customer(
        id="cust_hero", name="Rahul Sharma", email="rahul.sharma@example.com",
        phone="+919812345678", successful_payments=14, failed_payments=1,
        lifetime_value_paise=6_800_000, last_payment_at=now - timedelta(days=6),
        opted_out=False, reminders_sent=0, created_at=now - timedelta(days=400),
    )
    newbie = Customer(
        id="cust_new", name="Ananya Rao", email="ananya.rao@example.com",
        phone="+919887654321", successful_payments=0, failed_payments=0,
        lifetime_value_paise=0, last_payment_at=None,
        opted_out=False, reminders_sent=0, created_at=now - timedelta(days=2),
    )
    lapsed = Customer(
        id="cust_lapsed", name="Vikram Nair", email="vikram.nair@example.com",
        phone="+919800011122", successful_payments=1, failed_payments=5,
        lifetime_value_paise=180_000, last_payment_at=now - timedelta(days=95),
        opted_out=False, reminders_sent=3, created_at=now - timedelta(days=180),
    )
    db.add_all([hero, newbie, lapsed])
    db.flush()

    demo_txns = [
        # 1. Rs 7,500 UPI failure during a live bank outage -> delayed retry.
        dict(
            id="pay_demo_upi", razorpay_payment_id="pay_demo_upi", razorpay_order_id="order_demo_upi",
            customer_id=hero.id, amount_paise=750_000, method="upi", bank="HDFC",
            kind=TxnKind.PAYMENT, status=TxnStatus.FAILED, failure_reason="do_not_honour",
            created_at=now - timedelta(minutes=3), failed_at=now - timedelta(minutes=3),
            recovery_state=RecoveryState.DETECTED, at_risk=True,
            latent=Latent("temp_bank", 0.92, 6).to_dict(),
        ),
        # 2. Rs 24,999 card declined, no history -> alternative payment route.
        dict(
            id="pay_demo_card", razorpay_payment_id="pay_demo_card", razorpay_order_id="order_demo_card",
            customer_id=newbie.id, amount_paise=2_499_900, method="card", card_type="credit",
            bank="ICICI", kind=TxnKind.PAYMENT, status=TxnStatus.FAILED, failure_reason="card_declined",
            created_at=now - timedelta(minutes=9), failed_at=now - timedelta(minutes=9),
            recovery_state=RecoveryState.DETECTED, at_risk=True,
            latent=Latent("dead_instrument", 0.71, 0).to_dict(),
        ),
        # 3. Rs 1,999, five failures and three ignored reminders -> stop.
        dict(
            id="pay_demo_stop", razorpay_payment_id="pay_demo_stop", razorpay_order_id="order_demo_stop",
            customer_id=lapsed.id, amount_paise=199_900, method="card", card_type="debit",
            bank="SBI", kind=TxnKind.PAYMENT, status=TxnStatus.FAILED, failure_reason="insufficient_funds",
            created_at=now - timedelta(days=31), failed_at=now - timedelta(days=31),
            recovery_state=RecoveryState.DETECTED, at_risk=True, retry_count=2, outreach_count=3,
            latent=Latent("funds_persistent", 0.08, 0).to_dict(),
        ),
    ]
    for row in demo_txns:
        row.setdefault("currency", "INR")
        row.setdefault("recovered_amount_paise", 0)
        row.setdefault("retry_count", 0)
        row.setdefault("outreach_count", 0)
    db.bulk_insert_mappings(Transaction, demo_txns)

    # A live UPI outage: a burst of failures in the last few minutes, which is
    # what makes `recent_failure_spike` fire for scenario 1.
    burst = []
    for i in range(45):
        c = rng.choice(customers)
        minutes_ago = rng.randint(1, 9)
        latent = Latent("temp_bank", round(rng.uniform(0.5, 0.95), 3), rng.randint(4, 12))
        burst.append(dict(
            id=f"pay_burst_{i}", razorpay_payment_id=f"pay_burst_{i}",
            razorpay_order_id=f"order_burst_{i}", customer_id=c.id,
            amount_paise=_amount_paise(rng), currency="INR", method="upi", bank="HDFC",
            kind=TxnKind.PAYMENT, status=TxnStatus.FAILED,
            failure_reason=observe_failure_reason("temp_bank", rng),
            created_at=now - timedelta(minutes=minutes_ago),
            failed_at=now - timedelta(minutes=minutes_ago),
            recovery_state=RecoveryState.DETECTED, at_risk=True,
            latent=latent.to_dict(), recovered_amount_paise=0, retry_count=0, outreach_count=0,
        ))
    db.bulk_insert_mappings(Transaction, burst)
    return {"demo_scenarios": len(demo_txns), "outage_burst": len(burst)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the synthetic recovery dataset.")
    parser.add_argument("--transactions", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reset", action="store_true", help="wipe existing data first")
    parser.add_argument("--no-demo", dest="demo", action="store_false", help="skip pinned demo rows")
    parser.set_defaults(demo=True)
    args = parser.parse_args()

    stats = generate(total=args.transactions, seed=args.seed, reset=args.reset, demo=args.demo)
    print("Seeded synthetic dataset:")
    for k, v in stats.items():
        print(f"  {k:24} {v:,}")


if __name__ == "__main__":
    main()
