"""
Application Configuration

Loads settings from environment variables and YAML config files.
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Environment-based application settings."""

    # API Keys
    OPENAI_API_KEY: str = ""
    CLEARBIT_API_KEY: str = ""
    HUNTER_API_KEY: str = ""
    PROXYCURL_API_KEY: str = ""

    # CRM Integration
    HUBSPOT_API_KEY: str = ""
    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""

    # Slack
    SLACK_BOT_TOKEN: str = ""
    SLACK_CHANNEL_HOT: str = "#hot-leads"
    SLACK_CHANNEL_PIPELINE: str = "#sales-pipeline"

    # Redis (for caching and rate limiting)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Application
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    SCORING_CONFIG_PATH: str = "config/scoring.yaml"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


class ScoringConfig:
    """Scoring configuration loaded from YAML."""

    def __init__(self, config_path: Optional[str] = None):
        path = config_path or settings.SCORING_CONFIG_PATH
        self._config = self._load_config(path)

        self.icp = self._config.get("icp", {
            "company_size": [50, 10000],
            "industries": ["technology", "finance", "healthcare", "e-commerce"],
            "min_revenue": 1_000_000,
        })

        self.weights = self._config.get("scoring", {}).get("weights", {
            "company_fit": 0.35,
            "intent_signal": 0.30,
            "budget_indicator": 0.20,
            "urgency": 0.15,
        })

        self.routing = self._config.get("routing", {
            "hot": {"min_score": 80, "max_score": 100, "action": "route_to_ae"},
            "warm": {"min_score": 50, "max_score": 79, "action": "add_to_nurture"},
            "cold": {"min_score": 0, "max_score": 49, "action": "add_to_marketing"},
        })

    @staticmethod
    def _load_config(path: str) -> dict:
        """Load YAML config file, return empty dict if not found."""
        config_file = BASE_DIR / path
        if config_file.exists():
            with open(config_file) as f:
                return yaml.safe_load(f) or {}
        return {}


class SlackConfig(BaseModel):
    """Slack notification templates."""
    hot_lead_template: str = (
        ":fire: *HOT LEAD*\n"
        "Score: {score}/100\n"
        "Company: {company}\n"
        "Contact: {name} ({email})\n"
        "Reasoning: {reasoning}\n"
        "Action: {action}"
    )
    warm_lead_template: str = (
        ":large_yellow_circle: *Warm Lead*\n"
        "Score: {score}/100\n"
        "Company: {company}\n"
        "Added to nurture sequence."
    )


class CRMConfig(BaseModel):
    """CRM integration settings."""
    provider: str = "hubspot"  # hubspot or salesforce
    auto_create_contact: bool = True
    auto_create_deal: bool = True
    default_pipeline: str = "default"
    hot_deal_stage: str = "qualifiedtobuy"
    warm_deal_stage: str = "appointmentscheduled"


# Global settings instance
settings = Settings()
