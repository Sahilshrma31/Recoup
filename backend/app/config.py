"""Application settings and recovery policy limits.

Every money value is stored and computed in **paise** (integer) so that no
financial arithmetic in this codebase ever touches a float.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- infra ---
    database_url: str = "sqlite:///./data/recovery.db"
    api_prefix: str = "/api"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Anthropic (AI reasoning layer) ---
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    ai_enabled: bool = True
    ai_timeout_seconds: float = 45.0
    ai_max_tokens: int = 4096
    # Effort for the reasoning call. The heavy lifting is deterministic, so the
    # model is doing bounded disambiguation + explanation: "low" is plenty and
    # keeps per-transaction latency demo-usable. Raise for harder ambiguity.
    ai_effort: str = "low"
    ai_breaker_threshold: int = 3      # consecutive failures before tripping
    ai_breaker_cooldown_seconds: float = 60.0

    # --- Razorpay ---
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    # When False, executed actions resolve through the outcome simulator instead
    # of live polling. Auto-enabled when Razorpay keys are absent.
    razorpay_live: bool = False

    # --- recovery policy (deterministic guardrails, §29) ---
    auto_action_limit_paise: int = 1_000_000       # Rs 10,000 -> above this, merchant approval
    max_auto_retries: int = 2                     # automatic re-attempts per transaction
    max_outreach_attempts: int = 2                # reminders / links sent to a customer
    recovery_window_days: int = 14                # stop attempting after this long
    min_recovery_probability: float = 0.20        # below this -> NO_ACTION
    min_expected_value_paise: int = 5_000         # Rs 50 of expected value to justify acting
    retry_cooldown_minutes: int = 5               # min gap between two re-attempts

    # --- workers ---
    worker_enabled: bool = True
    #: SQLite serialises writes, so extra workers only add lock contention.
    #: Overridden automatically for a real database. See `worker_count`.
    workers: int | None = None
    scheduler_tick_seconds: float = 2.0
    # Demo speed-up: 1 simulated minute == this many real seconds.
    simulated_minute_seconds: float = 1.0

    @property
    def worker_count(self) -> int:
        if self.workers is not None:
            return max(1, self.workers)
        return 1 if self.database_url.startswith("sqlite") else 4

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def live_execution(self) -> bool:
        return self.razorpay_live and self.razorpay_configured

    @property
    def llm_configured(self) -> bool:
        return bool(self.ai_enabled and self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
