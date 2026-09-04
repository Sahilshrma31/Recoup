"""The AI reasoning layer -- and its failure containment.

Provider-agnostic: the model may be Claude or an NVIDIA-hosted model, selected
by `LLM_PROVIDER`. The differences live in `providers.py`; everything the agent
sees -- a validated `AgentAnalysis` or `LLMUnavailable` -- is identical.

The agent is designed so that this module is an *enhancement*, not a
dependency. Every path out of here either returns a validated `AgentAnalysis`
or raises `LLMUnavailable`, and the caller's response to `LLMUnavailable` is to
carry on with the deterministic decision. That is what makes the claim in the
pitch literally true: if the AI fails, the payment system does not fail with it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..config import Settings
from .prompts import SYSTEM_PROMPT, AgentAnalysis, build_user_message, sanitise_for_llm
from .providers import ProviderError, build_provider

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Raised for every failure mode: not configured, errored, refused, invalid."""


@dataclass(slots=True)
class ReasoningResult:
    analysis: AgentAnalysis
    model: str
    latency_ms: int
    request_id: str | None
    input_payload: dict


class _CircuitBreaker:
    """Stops hammering a model that is down.

    Without this, a provider outage turns every single transaction in the queue
    into a multi-second timeout. With it, the first few fail and the rest fall
    straight through to the deterministic path.
    """

    def __init__(self, threshold: int, cooldown_seconds: float) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()
            log.warning("AI reasoning circuit breaker opened for %.0fs", self._cooldown)

    def status(self) -> dict:
        return {"open": self.is_open, "consecutive_failures": self._failures}


class ReasoningClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._breaker = _CircuitBreaker(
            settings.ai_breaker_threshold, settings.ai_breaker_cooldown_seconds
        )
        self._provider = build_provider(settings)

    @property
    def available(self) -> bool:
        return self._provider is not None and not self._breaker.is_open

    def status(self) -> dict:
        return {
            "configured": self._provider is not None,
            "provider": self._settings.llm_provider,
            "model": self._settings.llm_model,
            "effort": self._settings.ai_effort,
            "structured_output_mode": getattr(self._provider, "_mode", "native"),
            **self._breaker.status(),
        }

    async def analyse(
        self,
        *,
        features: dict,
        diagnosis: dict,
        scored_actions: list[dict],
        deterministic_choice: str,
        policy_notes: list[str],
    ) -> ReasoningResult:
        if self._provider is None:
            raise LLMUnavailable(f"No API key configured for provider `{self._settings.llm_provider}`.")
        if self._breaker.is_open:
            raise LLMUnavailable("Reasoning circuit breaker is open after repeated failures.")

        safe_features = sanitise_for_llm(features)
        user_message = build_user_message(
            safe_features=safe_features,
            diagnosis=diagnosis,
            scored_actions=scored_actions,
            deterministic_choice=deterministic_choice,
            policy_notes=policy_notes,
        )

        started = time.monotonic()
        try:
            analysis, model = await self._provider.analyse(
                system=SYSTEM_PROMPT, user=user_message
            )
        except ProviderError as exc:
            # A permanent fault (bad key, no credits, safety decline) says
            # nothing about transient health, so it must not trip the breaker
            # towards a cooldown that would mask a later recovery.
            if not exc.permanent:
                self._breaker.record_failure()
            raise LLMUnavailable(str(exc)) from exc

        self._breaker.record_success()
        return ReasoningResult(
            analysis=analysis,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=None,
            input_payload={"features": safe_features, "scorecard_size": len(scored_actions)},
        )
