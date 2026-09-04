"""The AI reasoning layer -- and its failure containment.

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

import anthropic

from ..config import Settings
from .prompts import SYSTEM_PROMPT, AgentAnalysis, build_user_message, sanitise_for_llm

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
        self._client: anthropic.AsyncAnthropic | None = None
        if settings.llm_configured:
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.ai_timeout_seconds,
                max_retries=1,  # the agent's own fallback is faster than a long retry chain
            )

    @property
    def available(self) -> bool:
        return self._client is not None and not self._breaker.is_open

    def status(self) -> dict:
        return {
            "configured": self._client is not None,
            "model": self._settings.anthropic_model,
            "effort": self._settings.ai_effort,
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
        if self._client is None:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured.")
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
            response = await self._client.messages.parse(
                model=self._settings.anthropic_model,
                max_tokens=self._settings.ai_max_tokens,
                # Stable instructions first and cached; only the per-transaction
                # payload varies, so the prefix is reused across every call.
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
                thinking={"type": "adaptive"},
                output_config={"effort": self._settings.ai_effort},
                output_format=AgentAnalysis,
            )
        except anthropic.BadRequestError as exc:
            self._breaker.record_failure()
            raise LLMUnavailable(f"Malformed reasoning request: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            self._breaker.record_failure()
            raise LLMUnavailable("Anthropic API key was rejected.") from exc
        except anthropic.PermissionDeniedError as exc:
            self._breaker.record_failure()
            raise LLMUnavailable("Anthropic API key lacks permission for this model.") from exc
        except anthropic.NotFoundError as exc:
            self._breaker.record_failure()
            raise LLMUnavailable(f"Unknown model `{self._settings.anthropic_model}`.") from exc
        except anthropic.RateLimitError as exc:
            self._breaker.record_failure()
            retry_after = exc.response.headers.get("retry-after", "unknown")
            raise LLMUnavailable(f"Rate limited by Anthropic (retry after {retry_after}s).") from exc
        except anthropic.APITimeoutError as exc:
            self._breaker.record_failure()
            raise LLMUnavailable("Reasoning request timed out.") from exc
        except anthropic.APIConnectionError as exc:
            self._breaker.record_failure()
            raise LLMUnavailable("Could not reach the Anthropic API.") from exc
        except anthropic.APIStatusError as exc:
            self._breaker.record_failure()
            raise LLMUnavailable(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        # A safety decline is a normal, expected outcome -- not a crash. Fall
        # back to the deterministic decision exactly as for any other failure.
        if response.stop_reason == "refusal":
            self._breaker.record_success()  # the API is healthy; this request was declined
            category = getattr(response.stop_details, "category", None)
            raise LLMUnavailable(f"Model declined to analyse this transaction (category={category}).")

        analysis = response.parsed_output
        if analysis is None:
            self._breaker.record_failure()
            raise LLMUnavailable("Model returned no parseable structured output.")

        self._breaker.record_success()
        return ReasoningResult(
            analysis=analysis,
            model=response.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=response._request_id,
            input_payload={"features": safe_features, "scorecard_size": len(scored_actions)},
        )
