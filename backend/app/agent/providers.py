"""Provider adapters for the reasoning layer.

Each adapter takes a system prompt, a user message and the `AgentAnalysis`
schema, and returns a validated instance -- or raises `ProviderError`, which
the reasoning client turns into `LLMUnavailable` and the agent answers by
falling back to its deterministic decision.

The two providers are not equivalent in what they guarantee, and it matters:

* **Anthropic** validates the schema server-side via `messages.parse`, so a
  response either matches `AgentAnalysis` or does not arrive at all.
* **NVIDIA NIM** is OpenAI-compatible, and `response_format` support varies by
  model. Some honour a full JSON schema, some only "return JSON", some ignore
  the field and wrap JSON in prose. `NvidiaProvider` therefore negotiates
  downward and parses defensively.

Neither difference reaches the rest of the agent: both return the same object,
and both fail the same way.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from pydantic import ValidationError

from ..config import Settings
from .prompts import AgentAnalysis

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Any failure to obtain a valid analysis. `permanent` skips the breaker."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class ReasoningProvider(Protocol):
    name: str
    model: str

    async def analyse(self, *, system: str, user: str) -> tuple[AgentAnalysis, str]:
        """Return the analysis and the model id that produced it."""
        ...


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self._anthropic = anthropic
        self._settings = settings
        self.model = settings.anthropic_model
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.ai_timeout_seconds,
            max_retries=1,  # the agent's own fallback beats a long retry chain
        )

    async def analyse(self, *, system: str, user: str) -> tuple[AgentAnalysis, str]:
        anthropic = self._anthropic
        try:
            response = await self._client.messages.parse(
                model=self.model,
                max_tokens=self._settings.ai_max_tokens,
                # Stable instructions cached; only the transaction payload varies.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": self._settings.ai_effort},
                output_format=AgentAnalysis,
            )
        except anthropic.BadRequestError as exc:
            message = (exc.message or "").lower()
            if "credit balance" in message or "billing" in message:
                raise ProviderError(
                    "Anthropic credit balance is exhausted -- the key is valid but has no funds.",
                    permanent=True,
                ) from exc
            raise ProviderError(f"Malformed reasoning request: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic API key was rejected.", permanent=True) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError("Anthropic key lacks permission for this model.", permanent=True) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"Unknown model `{self.model}`.", permanent=True) from exc
        except anthropic.RateLimitError as exc:
            after = exc.response.headers.get("retry-after", "unknown")
            raise ProviderError(f"Rate limited by Anthropic (retry after {after}s).") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError("Reasoning request timed out.") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach the Anthropic API.") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        # A safety decline is an expected outcome, not a crash.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise ProviderError(f"Model declined this transaction (category={category}).", permanent=True)

        if response.parsed_output is None:
            raise ProviderError("Model returned no parseable structured output.")
        return response.parsed_output, response.model


# --------------------------------------------------------------------------
# NVIDIA NIM (OpenAI-compatible)
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a possibly chatty completion.

    Models without enforced structured output like to explain themselves before
    and after the JSON, or wrap it in a code fence. Rather than fail on that, we
    look for a fenced block first, then scan for the first balanced `{...}`.
    Reasoning models also emit <think> blocks, which are stripped.
    """
    if not text:
        raise ProviderError("Model returned an empty response.")

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    if match := _FENCE.search(cleaned):
        cleaned = match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start == -1:
        raise ProviderError("No JSON object found in the model response.")

    depth, in_string, escaped = 0, False, False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"Model response is not valid JSON: {exc}") from exc
    raise ProviderError("Model response contains an unterminated JSON object.")


class NvidiaProvider:
    """NVIDIA NIM via the OpenAI-compatible API.

    `response_format` support differs per model, so the first call negotiates:
    a strict JSON schema is tried first, and on rejection the provider steps
    down to plain JSON mode and then to prompt-only. Whatever mode succeeds is
    remembered, so the negotiation happens once rather than on every request.
    """

    name = "nvidia"
    _MODES = ("json_schema", "json_object", "prompt_only")

    def __init__(self, settings: Settings) -> None:
        from openai import AsyncOpenAI

        self._settings = settings
        self.model = settings.nvidia_model
        self._client = AsyncOpenAI(
            base_url=settings.nvidia_base_url,
            api_key=settings.nvidia_api_key,
            timeout=settings.ai_timeout_seconds,
            max_retries=0,
        )
        self._mode: str | None = None

    def _request_kwargs(self, mode: str, system: str, user: str) -> dict:
        kwargs: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self._settings.ai_max_tokens,
            # Zero temperature: this is a classification and a decision, not a
            # creative task. The same transaction must produce the same call
            # twice, and a merchant asking "why?" should get a stable answer.
            "temperature": 0.0,
        }
        if mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "AgentAnalysis",
                    "schema": AgentAnalysis.model_json_schema(),
                    "strict": True,
                },
            }
        elif mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    async def analyse(self, *, system: str, user: str) -> tuple[AgentAnalysis, str]:
        import openai

        modes = [self._mode] if self._mode else list(self._MODES)
        last_error: Exception | None = None

        for mode in modes:
            try:
                response = await self._client.chat.completions.create(
                    **self._request_kwargs(mode, system, user)
                )
            except openai.BadRequestError as exc:
                # Almost always "this model does not support that response_format".
                last_error = exc
                if self._mode is None:
                    log.info("NVIDIA model %s rejected %s; stepping down", self.model, mode)
                    continue
                raise ProviderError(f"NVIDIA rejected the request: {exc}") from exc
            except openai.AuthenticationError as exc:
                raise ProviderError("NVIDIA API key was rejected.", permanent=True) from exc
            except openai.PermissionDeniedError as exc:
                raise ProviderError("NVIDIA key lacks access to this model.", permanent=True) from exc
            except openai.NotFoundError as exc:
                raise ProviderError(f"Unknown NVIDIA model `{self.model}`.", permanent=True) from exc
            except openai.RateLimitError as exc:
                raise ProviderError("Rate limited by NVIDIA.") from exc
            except openai.APITimeoutError as exc:
                raise ProviderError("NVIDIA reasoning request timed out.") from exc
            except openai.APIConnectionError as exc:
                raise ProviderError("Could not reach the NVIDIA API.") from exc
            except openai.APIStatusError as exc:
                raise ProviderError(f"NVIDIA API error {exc.status_code}.") from exc

            content = (response.choices[0].message.content or "") if response.choices else ""
            try:
                payload = extract_json(content)
                analysis = AgentAnalysis.model_validate(payload)
            except (ProviderError, ValidationError) as exc:
                last_error = exc
                if self._mode is None and mode != self._MODES[-1]:
                    log.info("NVIDIA model %s gave unusable output in %s; stepping down", self.model, mode)
                    continue
                raise ProviderError(f"NVIDIA model returned unusable output: {exc}") from exc

            if self._mode != mode:
                self._mode = mode
                log.info("NVIDIA structured-output mode for %s: %s", self.model, mode)
            return analysis, response.model or self.model

        raise ProviderError(f"No usable structured-output mode for {self.model}: {last_error}")


def build_provider(settings: Settings) -> ReasoningProvider | None:
    if not settings.llm_configured:
        return None
    if settings.llm_provider == "nvidia":
        return NvidiaProvider(settings)
    return AnthropicProvider(settings)
