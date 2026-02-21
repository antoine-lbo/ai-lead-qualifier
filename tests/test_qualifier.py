"""
Tests for the AI Lead Qualifier pipeline.

Run with: pytest tests/ -v
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

from src.qualifier import (
    LeadQualifier,
    LeadInput,
    QualificationResult,
    ScoringWeights,
)
from src.enrichment import EnrichmentService, CompanyData
from src.router import LeadRouter, RoutingAction, RoutingResult


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def sample_lead():
    """A typical high-quality lead."""
    return LeadInput(
        email="john.smith@acme.com",
        company="Acme Corp",
        name="John Smith",
        message="Looking for automation solutions for our 50-person sales team.",
        source="website",
    )


@pytest.fixture
def cold_lead():
    """A low-quality lead."""
    return LeadInput(
        email="student@university.edu",
        company="",
        name="Test User",
        message="Just browsing for a research project.",
        source="organic",
    )


@pytest.fixture
def enriched_company():
    """Mock enrichment data for a good-fit company."""
    return CompanyData(
        name="Acme Corp",
        domain="acme.com",
        industry="technology",
        employee_count=250,
        estimated_revenue=25_000_000,
        location="San Francisco, CA",
        description="Enterprise SaaS platform for sales automation",
        tech_stack=["salesforce", "slack", "jira"],
        linkedin_url="https://linkedin.com/company/acme",
        founded_year=2015,
    )


@pytest.fixture
def qualifier():
    """LeadQualifier instance with default weights."""
    return LeadQualifier()


# ============================================================
# Scoring Weight Tests
# ============================================================


class TestScoringWeights:
    """Test that scoring weights are properly validated."""

    def test_default_weights_sum_to_one(self):
        weights = ScoringWeights(
            company_fit=0.35,
            intent_signal=0.30,
            budget_indicator=0.20,
            urgency=0.15,
        )
        total = (
            weights.company_fit
            + weights.intent_signal
            + weights.budget_indicator
            + weights.urgency
        )
        assert abs(total - 1.0) < 0.001

    def test_weights_must_be_positive(self):
        with pytest.raises(ValueError):
            ScoringWeights(
                company_fit=-0.5,
                intent_signal=0.5,
                budget_indicator=0.5,
                urgency=0.5,
            )


# ============================================================
# Qualification Pipeline Tests
# ============================================================


class TestLeadQualifier:
    """Test the main qualification pipeline."""

    @pytest.mark.asyncio
    async def test_hot_lead_scores_above_80(
        self, qualifier, sample_lead, enriched_company
    ):
        """Enterprise lead with budget should score HOT."""
        with patch.object(
            qualifier.enrichment_service,
            "enrich",
            new_callable=AsyncMock,
            return_value=enriched_company,
        ), patch.object(
            qualifier,
            "_analyze_with_ai",
            new_callable=AsyncMock,
            return_value={
                "company_fit": 90,
                "intent_signal": 85,
                "budget_indicator": 80,
                "urgency": 75,
                "reasoning": "Strong enterprise fit with clear buying signals",
            },
        ):
            result = await qualifier.qualify(sample_lead)

            assert isinstance(result, QualificationResult)
            assert result.score >= 80
            assert result.tier == "HOT"
            assert result.recommended_action == "route_to_ae"
            assert result.reasoning is not None

    @pytest.mark.asyncio
    async def test_cold_lead_scores_below_50(self, qualifier, cold_lead):
        """Student with no company should score COLD."""
        with patch.object(
            qualifier.enrichment_service,
            "enrich",
            new_callable=AsyncMock,
            return_value=CompanyData(
                name="Unknown",
                domain="university.edu",
                industry="education",
                employee_count=0,
                estimated_revenue=0,
            ),
        ), patch.object(
            qualifier,
            "_analyze_with_ai",
            new_callable=AsyncMock,
            return_value={
                "company_fit": 10,
                "intent_signal": 15,
                "budget_indicator": 5,
                "urgency": 10,
                "reasoning": "Non-commercial inquiry",
            },
        ):
            result = await qualifier.qualify(cold_lead)

            assert result.score < 50
            assert result.tier == "COLD"
            assert result.recommended_action == "add_to_marketing"

    @pytest.mark.asyncio
    async def test_enrichment_failure_handled_gracefully(self, qualifier, sample_lead):
        """Pipeline should still work if enrichment fails."""
        with patch.object(
            qualifier.enrichment_service,
            "enrich",
            new_callable=AsyncMock,
            side_effect=Exception("Clearbit API timeout"),
        ), patch.object(
            qualifier,
            "_analyze_with_ai",
            new_callable=AsyncMock,
            return_value={
                "company_fit": 50,
                "intent_signal": 60,
                "budget_indicator": 50,
                "urgency": 40,
                "reasoning": "Limited data available",
            },
        ):
            result = await qualifier.qualify(sample_lead)
            assert isinstance(result, QualificationResult)
            assert result.score > 0


# ============================================================
# Enrichment Service Tests
# ============================================================


class TestEnrichmentService:
    """Test lead enrichment from external sources."""

    @pytest.mark.asyncio
    async def test_enrichment_returns_company_data(self):
        service = EnrichmentService()
        with patch.object(
            service,
            "_fetch_clearbit",
            new_callable=AsyncMock,
            return_value={
                "name": "Test Corp",
                "domain": "test.com",
                "industry": "Technology",
                "metrics": {"employees": 500},
            },
        ):
            result = await service.enrich("user@test.com")
            assert isinstance(result, CompanyData)
            assert result.name == "Test Corp"

    @pytest.mark.asyncio
    async def test_cache_prevents_duplicate_api_calls(self):
        """Second call should use cache, not hit API."""
        service = EnrichmentService()
        mock_fetch = AsyncMock(
            return_value={
                "name": "Cached Corp",
                "domain": "cached.com",
                "industry": "Finance",
                "metrics": {"employees": 100},
            }
        )
        with patch.object(service, "_fetch_clearbit", mock_fetch):
            await service.enrich("user@cached.com")
            await service.enrich("user@cached.com")
            assert mock_fetch.call_count == 1


# ============================================================
# Router Tests
# ============================================================


class TestLeadRouter:
    """Test lead routing logic."""

    @pytest.mark.asyncio
    async def test_hot_lead_routed_to_ae(self):
        router = LeadRouter()
        result = await router.route(
            lead_data={"email": "vp@enterprise.com", "company": "Enterprise Inc"},
            qualification={"score": 92, "tier": "HOT"},
        )
        assert isinstance(result, RoutingResult)
        assert result.action == RoutingAction.ROUTE_TO_AE

    @pytest.mark.asyncio
    async def test_cold_lead_routed_to_marketing(self):
        router = LeadRouter()
        result = await router.route(
            lead_data={"email": "info@small.com"},
            qualification={"score": 20, "tier": "COLD"},
        )
        assert result.action == RoutingAction.ADD_TO_MARKETING

    @pytest.mark.asyncio
    async def test_warm_lead_added_to_nurture(self):
        router = LeadRouter()
        result = await router.route(
            lead_data={"email": "pm@midsize.com", "company": "MidSize Co"},
            qualification={"score": 65, "tier": "WARM"},
        )
        assert result.action == RoutingAction.ADD_TO_NURTURE
