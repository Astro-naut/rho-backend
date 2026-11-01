"""pydantic-settings for Rho v2. All secrets and tunables come from env / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(..., alias="DATABASE_URL")
    admin_api_key: str = Field("change-me-admin", alias="ADMIN_API_KEY")
    tick_secret: str = Field("change-me-tick", alias="TICK_SECRET")

    bolna_api_key: str = Field("", alias="BOLNA_API_KEY")
    bolna_agent_id_reactivation: str = Field("", alias="BOLNA_AGENT_ID_REACTIVATION")
    bolna_agent_id_referral: str = Field("", alias="BOLNA_AGENT_ID_REFERRAL")
    bolna_webhook_secret: str = Field("", alias="BOLNA_WEBHOOK_SECRET")
    bolna_api_base: str = Field("https://api.bolna.ai", alias="BOLNA_API_BASE")

    dry_run: bool = Field(True, alias="DRY_RUN")
    team_phones: str = Field("", alias="TEAM_PHONES")
    test_phone_allowlist: str = Field("", alias="TEST_PHONE_ALLOWLIST")

    inr_per_usd: float = Field(88.0, alias="INR_PER_USD")
    dev: bool = Field(False, alias="DEV")
    worker_enabled: bool = Field(True, alias="WORKER_ENABLED")
    tick_interval_seconds: int = Field(60, alias="TICK_INTERVAL_SECONDS")

    cors_extra_origins: str = Field("", alias="CORS_EXTRA_ORIGINS")

    rate_limit_capacity: float = Field(60.0, alias="RATE_LIMIT_CAPACITY")
    rate_limit_refill_per_sec: float = Field(1.0, alias="RATE_LIMIT_REFILL_PER_SEC")

    max_body_bytes: int = 4096

    @field_validator("dry_run", "dev", "worker_enabled", mode="before")
    @classmethod
    def _boolish(cls, v):  # noqa: ANN001
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    def team_phone_seed_list(self) -> List[str]:
        """Combine TEAM_PHONES and deprecated TEST_PHONE_ALLOWLIST for seeding."""
        parts: List[str] = []
        for raw in (self.team_phones, self.test_phone_allowlist):
            if not raw:
                continue
            parts.extend(p.strip() for p in raw.split(",") if p.strip())
        # dedupe preserving order
        seen: set[str] = set()
        out: List[str] = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def cors_extra_list(self) -> List[str]:
        if not self.cors_extra_origins.strip():
            return []
        return [o.strip() for o in self.cors_extra_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
