"""Test fixtures: an isolated in-memory database per test.

Nothing here touches the developer's SQLite file, so the suite can run while
the demo server is up.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.enums import RecoveryState, TxnKind, TxnStatus  # noqa: E402
from app.models import Customer, Transaction  # noqa: E402
from app.services.simulator import Latent  # noqa: E402


#: `_env_file=None` is load-bearing. Without it pydantic-settings reads the
#: developer's real .env, so whether a test passes depends on whose machine it
#: runs on -- and a suite that consults your credentials is not a suite.
def isolated_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture
def settings() -> Settings:
    return isolated_settings(
        database_url="sqlite://",
        anthropic_api_key=None,
        razorpay_live=False,
        auto_action_limit_paise=1_000_000,
        max_auto_retries=2,
        max_outreach_attempts=2,
        recovery_window_days=14,
        min_recovery_probability=0.20,
        min_expected_value_paise=5_000,
        simulated_minute_seconds=0.001,
    )


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def make_customer(db):
    def _make(cid="cust_1", *, successful=10, failed=1, opted_out=False, ltv=5_000_000):
        customer = Customer(
            id=cid, name="Test Customer", email=f"{cid}@example.com", phone="+919812345678",
            successful_payments=successful, failed_payments=failed,
            lifetime_value_paise=ltv, opted_out=opted_out,
            last_payment_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db.add(customer)
        db.flush()
        return customer
    return _make


@pytest.fixture
def make_transaction(db, make_customer):
    def _make(
        tid="pay_test_1", *, amount_paise=500_000, method="upi",
        failure_reason="upi_timeout", customer=None, kind=TxnKind.PAYMENT,
        retry_count=0, outreach_count=0, minutes_ago=2, latent=None,
    ):
        customer = customer or make_customer()
        failed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        txn = Transaction(
            id=tid, razorpay_payment_id=tid, razorpay_order_id=f"order_{tid}",
            customer_id=customer.id, amount_paise=amount_paise, currency="INR",
            method=method, kind=kind, status=TxnStatus.FAILED,
            failure_reason=failure_reason, failed_at=failed_at, created_at=failed_at,
            recovery_state=str(RecoveryState.DETECTED), at_risk=True,
            retry_count=retry_count, outreach_count=outreach_count,
            latent=(latent or Latent("temp_bank", 0.85, 6)).to_dict(),
        )
        db.add(txn)
        db.flush()
        return txn
    return _make


def features_for(txn, customer, **overrides):
    """Feature dict without needing a populated merchant context."""
    from app.agent.features import MerchantContext, compute_features

    base = compute_features(txn, customer, MerchantContext())
    base.update(overrides)
    return base
