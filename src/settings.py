"""Runtime configuration loaded from environment variables / .env file.

Distinct from `src/config.py`, which holds compile-time keyword maps and
analysis thresholds. This module is for *deployment* knobs — anything that
varies between dev, staging, and prod.

Loaded once at import time. Re-importing in tests is fine; pydantic-settings
caches nothing implicitly.
"""
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Override via env vars or .env file."""

    # ------------------------------------------------------------------ env
    env: Literal["dev", "staging", "prod"] = Field(
        default="dev",
        description="Deployment environment. Affects defaults for other settings.",
    )

    # ------------------------------------------------------------- logging
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_format: Literal["text", "json"] = "text"

    # ----------------------------------------------------------------- API
    api_key: Optional[str] = Field(
        default=None,
        description="If set, all /api/* endpoints require X-API-Key header.",
    )
    cors_origins: List[str] = Field(
        default_factory=lambda: ["*"],
        description="Allowed origins. Tighten in prod (e.g., your dashboard domain).",
    )
    rate_limit_default: str = Field(
        default="120/minute",
        description="Default rate limit per IP. slowapi syntax: '<n>/<unit>'.",
    )
    rate_limit_strict: str = Field(
        default="30/minute",
        description="Stricter limit for expensive endpoints (e.g., per-meeting drill-down).",
    )

    # ----------------------------------------------------------- pipeline
    dataset_path: Optional[Path] = Field(
        default=None,
        description="Override the default dataset location.",
    )
    pipeline_refresh_minutes: int = Field(
        default=0,
        ge=0,
        description="If > 0, the pipeline rebuilds itself every N minutes. 0 disables.",
    )

    # -------------------------------------------------------- observability
    metrics_enabled: bool = True
    sentry_dsn: Optional[str] = Field(
        default=None,
        description="If set, errors are forwarded to Sentry.",
    )
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    otel_endpoint: Optional[str] = Field(
        default=None,
        description="OTLP HTTP collector endpoint. If set, traces are exported.",
    )
    otel_service_name: str = "transcript-intelligence"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def auth_required(self) -> bool:
        return self.api_key is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings instance.

    `lru_cache` makes this effectively a singleton without thread-safety
    concerns (Python's import lock + the cache decorator handle it).
    """
    return Settings()
