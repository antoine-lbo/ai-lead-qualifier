"""
Environment Configuration Module

Centralized environment variable management with validation,
type coercion, and sensible defaults. Uses Pydantic Settings
for robust configuration handling across environments.

Supports:
  - .env file loading (development)
  - Environment variable overrides (production)
  - Type-safe configuration with validation
  - Multiple environment profiles (dev, staging, production)
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Environment(str, Enum):
    """Application environment profiles."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


class LogLevel(str, Enum):
    """Supported log levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Settings Classes
# ---------------------------------------------------------------------------

class OpenAISettings(BaseSettings):
    """OpenAI API configuration."""
    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: str = Field(default="", description="OpenAI API key")
    model: str = Field(default="gpt-4-turbo-preview", description="Model to use for qualification")
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    timeout: int = Field(default=30, ge=5, le=120, description="Request timeout in seconds")
    max_retries: int = Field(default=3, ge=0, le=10)


class DatabaseSettings(BaseSettings):
    """Database connection configuration."""
    model_config = SettingsConfigDict(env_prefix="DB_")

    url: str = Field(default="postgresql://localhost:5432/lead_qualifier")
    pool_min_size: int = Field(default=2, ge=1)
    pool_max_size: int = Field(default=10, ge=1)
    echo: bool = Field(default=False, description="Log SQL queries")
    ssl_mode: str = Field(default="prefer", description="SSL mode for connections")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgres://")):
            raise ValueError("Database URL must be a PostgreSQL connection string")
        return v


class RedisSettings(BaseSettings):
    """Redis connection configuration."""
    model_config = SettingsConfigDict(env_prefix="REDIS_")

    url: str = Field(default="redis://localhost:6379/0")
    password: str = Field(default="")
    ttl: int = Field(default=3600, ge=60, description="Default TTL in seconds")
    max_connections: int = Field(default=20, ge=1)


class EnrichmentSettings(BaseSettings):
    """Lead enrichment provider configuration."""
    model_config = SettingsConfigDict(env_prefix="ENRICHMENT_")

    clearbit_api_key: str = Field(default="", description="Clearbit API key")
    linkedin_api_key: str = Field(default="", description="LinkedIn API key")
    cache_ttl: int = Field(default=86400, description="Enrichment cache TTL (24h)")
    max_concurrent: int = Field(default=5, ge=1, le=20)
    timeout: int = Field(default=10, ge=1, le=60)

class SlackSettings(BaseSettings):
    """Slack notification configuration."""
    model_config = SettingsConfigDict(env_prefix="SLACK_")

    webhook_url: str = Field(default="", description="Slack webhook URL")
    channel: str = Field(default="#leads", description="Default notification channel")
    hot_lead_channel: str = Field(default="#hot-leads")
    enabled: bool = Field(default=True)
    mention_on_hot: bool = Field(default=True, description="@channel for hot leads")


class CRMSettings(BaseSettings):
    """CRM integration configuration."""
    model_config = SettingsConfigDict(env_prefix="CRM_")

    provider: str = Field(default="hubspot", description="CRM provider: hubspot or salesforce")
    api_key: str = Field(default="", description="CRM API key")
    base_url: str = Field(default="")
    sync_enabled: bool = Field(default=True)
    batch_size: int = Field(default=50, ge=1, le=200)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = {"hubspot", "salesforce"}
        if v.lower() not in allowed:
            raise ValueError(f"CRM provider must be one of: {allowed}")
        return v.lower()


class RateLimitSettings(BaseSettings):
    """Rate limiting configuration."""
    model_config = SettingsConfigDict(env_prefix="RATE_LIMIT_")

    requests_per_minute: int = Field(default=60, ge=1)
    requests_per_hour: int = Field(default=1000, ge=1)
    burst_size: int = Field(default=10, ge=1)
    enabled: bool = Field(default=True)


class ScoringSettings(BaseSettings):
    """Lead scoring configuration."""
    model_config = SettingsConfigDict(env_prefix="SCORING_")

    hot_threshold: int = Field(default=80, ge=0, le=100)
    warm_threshold: int = Field(default=50, ge=0, le=100)
    weight_company_fit: float = Field(default=0.35, ge=0.0, le=1.0)
    weight_intent_signal: float = Field(default=0.30, ge=0.0, le=1.0)
    weight_budget_indicator: float = Field(default=0.20, ge=0.0, le=1.0)
    weight_urgency: float = Field(default=0.15, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "ScoringSettings":
        total = (
            self.weight_company_fit
            + self.weight_intent_signal
            + self.weight_budget_indicator
            + self.weight_urgency
        )
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"Scoring weights must sum to 1.0, got {total:.2f}")
        return self

    @model_validator(mode="after")
    def validate_thresholds(self) -> "ScoringSettings":
        if self.warm_threshold >= self.hot_threshold:
            raise ValueError("warm_threshold must be less than hot_threshold")
        return self

# ---------------------------------------------------------------------------
# Main Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """
    Application settings — single source of truth for all configuration.

    Settings are loaded in this priority order:
      1. Environment variables (highest priority)
      2. .env file
      3. Default values (lowest priority)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = Field(default="AI Lead Qualifier")
    environment: Environment = Field(default=Environment.DEVELOPMENT)
    debug: bool = Field(default=False)
    log_level: LogLevel = Field(default=LogLevel.INFO)
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1, le=32)
    cors_origins: list[str] = Field(default=["http://localhost:3000"])

    # API
    api_prefix: str = Field(default="/api")
    api_key: str = Field(default="", description="API key for authenticating requests")
    api_key_header: str = Field(default="X-API-Key")

    # Sub-configurations
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    enrichment: EnrichmentSettings = Field(default_factory=EnrichmentSettings)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    crm: CRMSettings = Field(default_factory=CRMSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """Ensure critical settings are configured in production."""
        if self.environment == Environment.PRODUCTION:
            errors = []
            if not self.openai.api_key:
                errors.append("OPENAI_API_KEY is required in production")
            if not self.api_key:
                errors.append("API_KEY is required in production")
            if self.debug:
                errors.append("DEBUG must be False in production")
            if errors:                raise ValueError(f"Production validation failed: {'; '.join(errors)}")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.environment == Environment.DEVELOPMENT

    @property
    def is_testing(self) -> bool:
        return self.environment == Environment.TESTING

    def get_log_config(self) -> dict[str, Any]:
        """Return logging configuration dict."""
        return {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
                "json": {
                    "format": "%(message)s",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json" if self.is_production else "default",
                },
            },
            "root": {
                "level": self.log_level.value,
                "handlers": ["console"],
            },
        }

    def display(self) -> str:
        """Return a safe string representation (no secrets)."""
        safe = []
        safe.append(f"Environment: {self.environment.value}")
        safe.append(f"Debug: {self.debug}")
        safe.append(f"Host: {self.host}:{self.port}")
        safe.append(f"Workers: {self.workers}")
        safe.append(f"OpenAI Model: {self.openai.model}")
        safe.append(f"CRM Provider: {self.crm.provider}")
        safe.append(f"Slack Enabled: {self.slack.enabled}")
        safe.append(f"Rate Limit: {self.rate_limit.requests_per_minute}/min")
        return "\n".join(safe)


# ---------------------------------------------------------------------------
# Singleton Access
# ---------------------------------------------------------------------------

@lru_cache
def get_settings() -> Settings:
    """
    Return cached application settings.

    Settings are loaded once and cached for the lifetime of the process.
    To reload settings (e.g., in tests), call `get_settings.cache_clear()`.
    """
    return Settings()
