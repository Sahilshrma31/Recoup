"""Recovery state machine (design §15).

Enforcing transitions in one place is what stops the workflow from drifting
into impossible states -- executing a transaction that was already recovered,
or re-planning one the agent deliberately stopped.
"""
from __future__ import annotations

import logging
from collections import deque

from ..enums import STATE_TRANSITIONS, RecoveryState
from ..models import Transaction

log = logging.getLogger(__name__)


class InvalidTransition(RuntimeError):
    def __init__(self, txn_id: str, frm: RecoveryState, to: RecoveryState) -> None:
        super().__init__(f"{txn_id}: illegal recovery transition {frm} -> {to}")
        self.frm, self.to = frm, to


def current_state(txn: Transaction) -> RecoveryState:
    return RecoveryState(txn.recovery_state)


def can_transition(frm: RecoveryState, to: RecoveryState) -> bool:
    return to in STATE_TRANSITIONS.get(frm, frozenset())


def transition(txn: Transaction, to: RecoveryState, *, reason: str | None = None) -> RecoveryState:
    frm = current_state(txn)
    if frm is to:
        return to
    if not can_transition(frm, to):
        raise InvalidTransition(txn.id, frm, to)
    txn.recovery_state = str(to)
    if to is RecoveryState.STOPPED:
        txn.stop_reason = reason
        txn.at_risk = False
    elif to is RecoveryState.RECOVERED:
        txn.at_risk = False
    log.debug("%s: %s -> %s", txn.id, frm, to)
    return to


def path_to(frm: RecoveryState, to: RecoveryState) -> list[RecoveryState] | None:
    """Shortest legal sequence of states from `frm` to `to`, or None."""
    if frm is to:
        return []
    queue: deque[tuple[RecoveryState, list[RecoveryState]]] = deque([(frm, [])])
    seen = {frm}
    while queue:
        state, route = queue.popleft()
        for nxt in STATE_TRANSITIONS.get(state, frozenset()):
            if nxt in seen:
                continue
            step = route + [nxt]
            if nxt is to:
                return step
            seen.add(nxt)
            queue.append((nxt, step))
    return None


def advance_to(txn: Transaction, to: RecoveryState, *, reason: str | None = None) -> RecoveryState:
    """Move to `to` through the intermediate states the machine requires.

    Callers that legitimately need a transaction to end up in a given state
    should not have to know the route. Jumping straight there would either
    raise or, worse, silently skip a state the audit trail depends on -- so
    this walks the shortest legal path instead.
    """
    route = path_to(current_state(txn), to)
    if route is None:
        raise InvalidTransition(txn.id, current_state(txn), to)
    for step in route:
        transition(txn, step, reason=reason if step is to else None)
    return to
