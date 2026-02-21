"""
Shared test fixtures and configuration for the AI Lead Qualifier test suite.

Provides reusable fixtures for FastAPI test client, mock services,
sample data, and environment configuration.
"""

import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from httpx import AsyncClient

# Ensure test environment variables are set before importing app modules
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake-key-for-testing")
os.environ.setdefault("CLEARBIT_API_KEY", "test_clearbit_key")
os.environ.setdefault("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("ENVIRONMENT", "test")

from src.main import app


# ---------------------------------------------------------------------------
# Event Loop
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop():
    """Create a shared event loop for all async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# HTTP Clients
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Synchronous FastAPI test client."""
    return TestClient(app)


@pytest.fixture
async def async_client():
    """Async HTTP client for testing async endpoints."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Sample Lead Data
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_lead():
    """A standard inbound lead payload."""
    return {
        "email": "jane@acmecorp.com",
        "company": "Acme Corporation",
        "message": "We need an automation platform for our 200-person sales team. Budget approved for Q1.",
        "name": "Jane Smith",
        "phone": "+1-555-0142",
    }


@pytest.fixture
def sample_lead_minimal():
    """Lead with only required fields."""
    return {
        "email": "test@startup.io",
        "company": "StartupIO",
        "message": "Interested in your product",
    }


@pytest.fixture
def sample_lead_enterprise():
    """High-value enterprise lead."""
    return {
        "email": "cto@fortune500.com",
        "company": "Fortune 500 Inc",
        "message": "Looking for enterprise-grade AI scoring for 5,000+ leads/month. Need SOC2 compliance. $500K budget range.",
        "name": "Michael Chen",
        "phone": "+1-555-0199",
    }


@pytest.fixture
def sample_lead_cold():
    """Low-quality lead likely to score cold."""
    return {
        "email": "student@university.edu",
        "company": "University Project",
        "message": "Just browsing for a school project",
    }


# ---------------------------------------------------------------------------
# Mock Qualification Results
# ---------------------------------------------------------------------------

@pytest.fixture
def hot_qualification_result():
    """Qualification result for a hot lead."""
    return {
        "score": 92,
        "tier": "HOT",
        "reasoning": "Enterprise company with clear pain point, budget confirmed, decision-maker contact",
        "recommended_action": "route_to_ae",
        "enrichment": {
            "company_size": "200-500",
            "industry": "Technology",
            "estimated_revenue": "$50M-$100M",
            "linkedin_url": "https://linkedin.com/company/acmecorp",
        },
    }


@pytest.fixture
def warm_qualification_result():
    """Qualification result for a warm lead."""
    return {
        "score": 62,
        "tier": "WARM",
        "reasoning": "Mid-market company, some interest signals but no clear budget indicator",
        "recommended_action": "add_to_nurture",
        "enrichment": {
            "company_size": "50-200",
            "industry": "E-commerce",
            "estimated_revenue": "$5M-$10M",
        },
    }


@pytest.fixture
def cold_qualification_result():
    """Qualification result for a cold lead."""
    return {
        "score": 18,
        "tier": "COLD",
        "reasoning": "Not a business entity, no budget or intent signals",
        "recommended_action": "add_to_marketing",
        "enrichment": {
            "company_size": "1-10",
            "industry": "Education",
            "estimated_revenue": "< $1M",
        },
    }


# ---------------------------------------------------------------------------
# Mock External Services
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_openai():
    """Mock OpenAI API responses."""
    with patch("src.qualifier.openai_client") as mock:
        mock.chat.completions.create = AsyncMock(
            return_value=MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content='{"score": 85, "reasoning": "Strong enterprise fit", "tier": "HOT"}'
                        )
                    )
                ]
            )
        )
        yield mock


@pytest.fixture
def mock_clearbit():
    """Mock Clearbit enrichment API."""
    with patch("src.enrichment.fetch_clearbit_data") as mock:
        mock.return_value = {
            "company": {
                "name": "Acme Corporation",
                "domain": "acmecorp.com",
                "metrics": {
                    "employees": 350,
                    "estimatedAnnualRevenue": "$50M-$100M",
                },
                "category": {"industry": "Technology"},
                "linkedin": {"handle": "acmecorp"},
            }
        }
        yield mock


@pytest.fixture
def mock_slack():
    """Mock Slack webhook notifications."""
    with patch("src.slack_notifier.send_slack_notification") as mock:
        mock.return_value = {"ok": True}
        yield mock


@pytest.fixture
def mock_redis():
    """Mock Redis for rate limiting and caching."""
    with patch("src.rate_limiter.redis_client") as mock:
        mock.get = AsyncMock(return_value=None)
        mock.set = AsyncMock(return_value=True)
        mock.incr = AsyncMock(return_value=1)
        mock.expire = AsyncMock(return_value=True)
        yield mock


# ---------------------------------------------------------------------------
# Scoring Configuration
# ---------------------------------------------------------------------------

@pytest.fixture
def scoring_config():
    """Test scoring configuration matching config/scoring.yaml."""
    return {
        "icp": {
            "company_size": [50, 10000],
            "industries": ["technology", "finance", "healthcare", "e-commerce"],
            "min_revenue": 1000000,
        },
        "scoring": {
            "weights": {
                "company_fit": 0.35,
                "intent_signal": 0.30,
                "budget_indicator": 0.20,
                "urgency": 0.15,
            }
        },
        "routing": {
            "hot": {"min_score": 80, "action": "route_to_ae"},
            "warm": {"min_score": 50, "action": "add_to_nurture"},
            "cold": {"min_score": 0, "action": "add_to_marketing"},
        },
    }


# ---------------------------------------------------------------------------
# Utility Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_csv_content():
    """Valid CSV content for batch processing tests."""
    return (
        "email,company,message,name,phone\n"
        "alice@acme.com,Acme Corp,Need automation for 200 employees,Alice Smith,555-0101\n"
        "bob@startup.io,StartupIO,Looking for AI scoring,Bob Jones,\n"
        "carol@bigcorp.com,BigCorp Inc,Enterprise deal 500 seats,Carol Lee,555-0303\n"
    )


@pytest.fixture(autouse=True)
def reset_job_store():
    """Clear the in-memory job store between tests."""
    from src.batch import _jobs
    _jobs.clear()
    yield
    _jobs.clear()
