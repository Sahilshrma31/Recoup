"""Event-driven runtime (design §25).

A webhook lands, gets queued, and a worker runs the agent against it. A second
loop ticks the clock: it executes delayed retries when they come due, verifies
attempts whose outcome is now knowable, and re-plans transactions whose last
attempt failed.

The queue is in-process, which is the right size for this system today and the
wrong size for production -- swapping it for SQS/Kafka means replacing
`publish`/`_worker` and nothing else.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import session_scope
from ..enums import Action, AttemptStatus, RecoveryState, TxnStatus
from ..agent.llm import ReasoningClient
from ..agent.orchestrator import ActionInFlight, AlreadyResolved, RecoveryAgent
from ..models import RecoveryAttempt, Transaction, utcnow
from ..razorpay_client.base import PaymentGateway
from ..razorpay_client.factory import build_gateway
from . import activity
from .executor import ActionExecutor
from .verification import VerificationService

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RecoveryEvent:
    """One thing that happened and might put revenue at risk."""

    type: str                 # payment.failed | checkout.abandoned | subscription.charge.failed
    transaction_id: str
    source: str = "webhook"


class AgentRuntime:
    """Owns the agent's moving parts and their background tasks."""

    def __init__(self, settings: Settings, gateway: PaymentGateway | None = None) -> None:
        self.settings = settings
        self.gateway = gateway or build_gateway(settings)
        self.reasoning = ReasoningClient(settings)
        self.agent = RecoveryAgent(settings, self.reasoning)
        self.executor = ActionExecutor(settings, self.gateway)
        self.verifier = VerificationService(settings, self.gateway)

        self.queue: asyncio.Queue[RecoveryEvent] = asyncio.Queue(maxsize=10_000)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.processed = 0
        self.errors = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self, *, workers: int = 2) -> None:
        if self._running:
            return
        self._running = True
        for i in range(workers):
            self._tasks.append(asyncio.create_task(self._worker(i), name=f"recovery-worker-{i}"))
        self._tasks.append(asyncio.create_task(self._scheduler(), name="recovery-scheduler"))
        log.info("Recovery runtime started (%d workers, gateway=%s)", workers, self.gateway.name)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("Recovery runtime stopped")

    def status(self) -> dict:
        return {
            "running": self._running,
            "queue_depth": self.queue.qsize(),
            "processed": self.processed,
            "errors": self.errors,
            "gateway": self.gateway.name,
            "live_execution": self.settings.live_execution,
            "ai": self.reasoning.status(),
        }

    # -- ingestion ---------------------------------------------------------

    async def publish(self, event: RecoveryEvent) -> None:
        await self.queue.put(event)

    # -- the agent pass ----------------------------------------------------

    async def process(self, db: Session, txn: Transaction, *, merchant_approved: bool = False) -> None:
        """One full Detect -> ... -> Act pass over a single transaction."""
        try:
            result = await self.agent.analyse(db, txn, merchant_approved=merchant_approved)
        except (ActionInFlight, AlreadyResolved) as exc:
            # Normal in a queue: a webhook can arrive for a transaction the
            # scheduler is already acting on. Skip it rather than kill the worker.
            log.debug("skipping %s: %s", txn.id, exc)
            return None
        if RecoveryState(txn.recovery_state) is RecoveryState.AWAITING_APPROVAL:
            return  # a human decides from here
        await self.executor.execute(db, txn, result.decision)

    async def _worker(self, index: int) -> None:
        while self._running:
            try:
                event = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                with session_scope() as db:
                    txn = db.get(Transaction, event.transaction_id)
                    if txn is None:
                        log.warning("Event for unknown transaction %s", event.transaction_id)
                        continue
                    if not txn.is_recoverable_target:
                        continue
                    await self.process(db, txn)
                self.processed += 1
            except asyncio.CancelledError:
                return
            except Exception:
                self.errors += 1
                log.exception("Worker %d failed on %s", index, event.transaction_id)
            finally:
                self.queue.task_done()

    # -- the clock ---------------------------------------------------------

    async def _scheduler(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.settings.scheduler_tick_seconds)
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception:
                self.errors += 1
                log.exception("Scheduler tick failed")

    async def _tick(self) -> None:
        await self._run_due_attempts()
        await self._verify_due_attempts()
        await self._replan_failed()

    async def _run_due_attempts(self) -> None:
        """Fire delayed retries whose cooldown has elapsed."""
        with session_scope() as db:
            due = db.execute(
                select(RecoveryAttempt)
                .where(
                    RecoveryAttempt.status == AttemptStatus.SCHEDULED,
                    RecoveryAttempt.scheduled_for.is_not(None),
                    RecoveryAttempt.scheduled_for <= utcnow(),
                )
                .limit(25)
            ).scalars().all()
            for attempt in due:
                txn = attempt.transaction
                if not txn.is_recoverable_target:
                    attempt.status = AttemptStatus.BLOCKED
                    continue
                activity.emit(
                    db, transaction_id=txn.id, stage="act",
                    message="Cooldown elapsed -- executing the scheduled re-attempt.",
                    detail={"attempt_id": attempt.id},
                )
                await self.executor._perform(db, txn, attempt)

    async def _verify_due_attempts(self) -> None:
        with session_scope() as db:
            for attempt in self.verifier.due_attempts(db):
                await self.verifier.verify(db, attempt)

    async def _replan_failed(self) -> None:
        """A failed attempt is not the end -- decide whether to try again or stop."""
        with session_scope() as db:
            stuck = db.execute(
                select(Transaction)
                .where(Transaction.recovery_state == RecoveryState.ATTEMPT_FAILED)
                .limit(10)
            ).scalars().all()
            for txn in stuck:
                await self.process(db, txn)


_runtime: AgentRuntime | None = None


def get_runtime() -> AgentRuntime:
    if _runtime is None:
        raise RuntimeError("Agent runtime has not been started.")
    return _runtime


def set_runtime(runtime: AgentRuntime | None) -> None:
    global _runtime
    _runtime = runtime
