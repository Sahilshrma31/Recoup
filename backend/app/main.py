"""Recoup -- API entrypoint.

An autonomous revenue recovery agent for Razorpay merchants.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import analytics, decisions, demo, recovery, stream, transactions, webhooks
from .config import settings
from .db import init_db
from .services.runtime import AgentRuntime, set_runtime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("recoup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    runtime = AgentRuntime(settings)
    set_runtime(runtime)
    if settings.worker_enabled:
        await runtime.start(workers=settings.worker_count)
    log.info(
        "Recovery agent ready | gateway=%s live=%s ai=%s model=%s",
        runtime.gateway.name, settings.live_execution,
        settings.llm_configured, settings.anthropic_model,
    )
    try:
        yield
    finally:
        await runtime.stop()
        set_runtime(None)


app = FastAPI(
    title="Recoup — Razorpay Revenue Recovery Agent",
    version="1.0.0",
    description=(
        "An autonomous agent that diagnoses failed payments, predicts recovery "
        "probability, chooses a recovery action within deterministic guardrails, "
        "executes it, and verifies the money arrived."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    transactions.router, recovery.router, analytics.router,
    decisions.router, webhooks.router, stream.router, demo.router,
):
    app.include_router(router, prefix=settings.api_prefix)


@app.get("/health", tags=["ops"])
def health():
    """Deployment posture, stated plainly so demo mode is never mistaken for live."""
    from .services.runtime import get_runtime

    try:
        runtime_status = get_runtime().status()
    except RuntimeError:
        runtime_status = {"running": False}

    return {
        "status": "ok",
        "execution_mode": "live-razorpay" if settings.live_execution else "simulated",
        "razorpay_configured": settings.razorpay_configured,
        "webhook_signature_verification": bool(settings.razorpay_webhook_secret),
        "ai_configured": settings.llm_configured,
        "model": settings.anthropic_model if settings.llm_configured else None,
        "policy": {
            "auto_action_limit_rupees": settings.auto_action_limit_paise / 100,
            "max_auto_retries": settings.max_auto_retries,
            "max_outreach_attempts": settings.max_outreach_attempts,
            "recovery_window_days": settings.recovery_window_days,
            "min_recovery_probability": settings.min_recovery_probability,
        },
        "runtime": runtime_status,
    }
