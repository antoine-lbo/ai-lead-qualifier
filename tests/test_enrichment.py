"""
Tests for the lead enrichment module.

Covers:
  - Clearbit enrichment provider
  - LinkedIn enrichment provider
  - Public data enrichment (fallback)
  - Enrichment pipeline orchestration
  - Caching behavior
  - Error handling and retries
  - Rate limiting compliance
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from src.enrichment import (
    EnrichmentPipeline,
    ClearbitProvider,
    LinkedInProvider,
    PublicDataProvider,
    EnrichmentResult,
    EnrichmentError,
)


# ---------- Fixtures ----------


@pytest.fixture
def sample_lead():
    """A typical inbound lead for testing."""
    return {
        "email": "jane.doe@acmecorp.com",
        "company": "Acme Corp",
        "name": "Jane Doe",
        "message": "Looking for automation solutions for our 50-person sales team",
        "source": "website_form",
    }


@pytest.fixture
def sample_enrichment_data():
    """Mock enrichment data returned by Clearbit."""
    return {
        "company_name": "Acme Corp",
        "domain": "acmecorp.com",
        "industry": "Technology",
        "employee_count": 150,
        "estimated_revenue": "$10M-$50M",
        "founded_year": 2015,
        "location": {
            "city": "San Francisco",
            "state": "CA",
            "country": "US",
        },
        "tech_stack": ["Salesforce", "Slack", "AWS"],
        "social": {
            "linkedin": "https://linkedin.com/company/acmecorp",
            "twitter": "https://twitter.com/acmecorp",
        },
        "person": {
            "title": "VP of Sales",
            "seniority": "executive",
            "department": "sales",
        },
    }


@pytest.fixture
def mock_redis():
    """Mock Redis client for caching tests."""
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.return_value = True
    redis.exists.return_value = False
    return redis


@pytest.fixture
def clearbit_provider():
    """Clearbit provider with mocked HTTP client."""
    provider = ClearbitProvider(api_key="test_clearbit_key")
    provider._client = AsyncMock()
    return provider


@pytest.fixture
def linkedin_provider():
    """LinkedIn provider with mocked HTTP client."""
    provider = LinkedInProvider(api_key="test_linkedin_key")
    provider._client = AsyncMock()
    return provider


@pytest.fixture
def enrichment_pipeline(mock_redis):
    """Full enrichment pipeline with all providers mocked."""
    pipeline = EnrichmentPipeline(
        clearbit_key="test_clearbit",
        linkedin_key="test_linkedin",
        redis_client=mock_redis,
        cache_ttl=3600,
    )
    return pipeline

# ---------- Clearbit Provider Tests ----------


class TestClearbitProvider:
    """Tests for the Clearbit enrichment provider."""

    @pytest.mark.asyncio
    async def test_enrich_success(self, clearbit_provider, sample_lead, sample_enrichment_data):
        """Should return enrichment data for a valid company email."""
        clearbit_provider._client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: sample_enrichment_data,
        )

        result = await clearbit_provider.enrich(sample_lead["email"], sample_lead["company"])

        assert isinstance(result, EnrichmentResult)
        assert result.company_name == "Acme Corp"
        assert result.industry == "Technology"
        assert result.employee_count == 150
        assert result.estimated_revenue == "$10M-$50M"
        assert result.source == "clearbit"

    @pytest.mark.asyncio
    async def test_enrich_company_not_found(self, clearbit_provider, sample_lead):
        """Should return None when company is not in Clearbit database."""
        clearbit_provider._client.get.return_value = MagicMock(
            status_code=404,
            json=lambda: {"error": "Company not found"},
        )

        result = await clearbit_provider.enrich(sample_lead["email"], sample_lead["company"])

        assert result is None

    @pytest.mark.asyncio
    async def test_enrich_rate_limited(self, clearbit_provider, sample_lead):
        """Should raise EnrichmentError on rate limit (429)."""
        clearbit_provider._client.get.return_value = MagicMock(
            status_code=429,
            headers={"Retry-After": "60"},
        )

        with pytest.raises(EnrichmentError) as exc_info:
            await clearbit_provider.enrich(sample_lead["email"], sample_lead["company"])

        assert "rate limit" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_enrich_api_error(self, clearbit_provider, sample_lead):
        """Should raise EnrichmentError on server error (5xx)."""
        clearbit_provider._client.get.return_value = MagicMock(
            status_code=500,
            json=lambda: {"error": "Internal server error"},
        )

        with pytest.raises(EnrichmentError):
            await clearbit_provider.enrich(sample_lead["email"], sample_lead["company"])

    @pytest.mark.asyncio
    async def test_enrich_timeout(self, clearbit_provider, sample_lead):
        """Should handle request timeout gracefully."""
        import asyncio
        clearbit_provider._client.get.side_effect = asyncio.TimeoutError()

        with pytest.raises(EnrichmentError) as exc_info:
            await clearbit_provider.enrich(sample_lead["email"], sample_lead["company"])

        assert "timeout" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_enrich_invalid_email_domain(self, clearbit_provider):
        """Should skip enrichment for free email domains."""
        result = await clearbit_provider.enrich("user@gmail.com", "Unknown")

        assert result is None
        clearbit_provider._client.get.assert_not_called()

    def test_free_email_detection(self, clearbit_provider):
        """Should correctly identify free email providers."""
        assert clearbit_provider._is_free_email("user@gmail.com") is True
        assert clearbit_provider._is_free_email("user@yahoo.com") is True
        assert clearbit_provider._is_free_email("user@hotmail.com") is True
        assert clearbit_provider._is_free_email("user@outlook.com") is True
        assert clearbit_provider._is_free_email("ceo@acmecorp.com") is False
        assert clearbit_provider._is_free_email("sales@enterprise.io") is False

# ---------- LinkedIn Provider Tests ----------


class TestLinkedInProvider:
    """Tests for the LinkedIn enrichment provider."""

    @pytest.mark.asyncio
    async def test_enrich_success(self, linkedin_provider, sample_lead):
        """Should return person data from LinkedIn."""
        linkedin_provider._client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "title": "VP of Sales",
                "seniority": "executive",
                "connections": 500,
                "profile_url": "https://linkedin.com/in/janedoe",
            },
        )

        result = await linkedin_provider.enrich(sample_lead["email"], sample_lead["company"])

        assert result is not None
        assert result.person_title == "VP of Sales"
        assert result.person_seniority == "executive"
        assert result.source == "linkedin"

    @pytest.mark.asyncio
    async def test_enrich_profile_not_found(self, linkedin_provider, sample_lead):
        """Should return None when LinkedIn profile not found."""
        linkedin_provider._client.get.return_value = MagicMock(status_code=404)

        result = await linkedin_provider.enrich(sample_lead["email"], sample_lead["company"])

        assert result is None

    @pytest.mark.asyncio
    async def test_enrich_auth_failure(self, linkedin_provider, sample_lead):
        """Should raise EnrichmentError on auth failure (401)."""
        linkedin_provider._client.get.return_value = MagicMock(status_code=401)

        with pytest.raises(EnrichmentError) as exc_info:
            await linkedin_provider.enrich(sample_lead["email"], sample_lead["company"])

        assert "authentication" in str(exc_info.value).lower()


# ---------- Pipeline Orchestration Tests ----------


class TestEnrichmentPipeline:
    """Tests for the enrichment pipeline orchestrator."""

    @pytest.mark.asyncio
    async def test_full_enrichment_pipeline(self, enrichment_pipeline, sample_lead, sample_enrichment_data):
        """Should run all providers and merge results."""
        with patch.object(
            enrichment_pipeline._clearbit, "enrich",
            return_value=EnrichmentResult(
                company_name="Acme Corp",
                industry="Technology",
                employee_count=150,
                estimated_revenue="$10M-$50M",
                source="clearbit",
            ),
        ), patch.object(
            enrichment_pipeline._linkedin, "enrich",
            return_value=EnrichmentResult(
                person_title="VP of Sales",
                person_seniority="executive",
                source="linkedin",
            ),
        ):
            result = await enrichment_pipeline.enrich(sample_lead)

        assert result.company_name == "Acme Corp"
        assert result.industry == "Technology"
        assert result.employee_count == 150
        assert result.person_title == "VP of Sales"
        assert result.person_seniority == "executive"
        assert "clearbit" in result.sources
        assert "linkedin" in result.sources
    @pytest.mark.asyncio
    async def test_clearbit_failure_falls_back_to_public(self, enrichment_pipeline, sample_lead):
        """Should fall back to public data when Clearbit fails."""
        with patch.object(
            enrichment_pipeline._clearbit, "enrich",
            side_effect=EnrichmentError("Clearbit API unavailable"),
        ), patch.object(
            enrichment_pipeline._public, "enrich",
            return_value=EnrichmentResult(
                company_name="Acme Corp",
                industry="Technology",
                source="public",
            ),
        ), patch.object(
            enrichment_pipeline._linkedin, "enrich",
            return_value=None,
        ):
            result = await enrichment_pipeline.enrich(sample_lead)

        assert result.company_name == "Acme Corp"
        assert "public" in result.sources
        assert "clearbit" not in result.sources

    @pytest.mark.asyncio
    async def test_all_providers_fail(self, enrichment_pipeline, sample_lead):
        """Should return minimal result when all providers fail."""
        with patch.object(
            enrichment_pipeline._clearbit, "enrich",
            side_effect=EnrichmentError("Clearbit down"),
        ), patch.object(
            enrichment_pipeline._linkedin, "enrich",
            side_effect=EnrichmentError("LinkedIn down"),
        ), patch.object(
            enrichment_pipeline._public, "enrich",
            return_value=None,
        ):
            result = await enrichment_pipeline.enrich(sample_lead)

        assert result is not None
        assert result.company_name == "Acme Corp"  # From lead data
        assert len(result.sources) == 0
        assert result.enrichment_failed is True

    @pytest.mark.asyncio
    async def test_cache_hit_skips_providers(self, enrichment_pipeline, sample_lead, mock_redis):
        """Should return cached result without calling providers."""
        import json
        cached = EnrichmentResult(
            company_name="Acme Corp",
            industry="Technology",
            employee_count=150,
            source="clearbit",
            sources=["clearbit"],
        )
        mock_redis.get.return_value = json.dumps(cached.dict())

        with patch.object(
            enrichment_pipeline._clearbit, "enrich"
        ) as mock_clearbit:
            result = await enrichment_pipeline.enrich(sample_lead)

        mock_clearbit.assert_not_called()
        assert result.company_name == "Acme Corp"
        assert result.industry == "Technology"

    @pytest.mark.asyncio
    async def test_cache_miss_calls_providers_and_caches(self, enrichment_pipeline, sample_lead, mock_redis):
        """Should call providers on cache miss and store result."""
        mock_redis.get.return_value = None

        with patch.object(
            enrichment_pipeline._clearbit, "enrich",
            return_value=EnrichmentResult(
                company_name="Acme Corp",
                industry="Technology",
                source="clearbit",
            ),
        ), patch.object(
            enrichment_pipeline._linkedin, "enrich",
            return_value=None,
        ):
            result = await enrichment_pipeline.enrich(sample_lead)

        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert "acmecorp.com" in call_args[0][0]  # Cache key contains domain
        assert call_args[1]["ex"] == 3600  # TTL matches config
    @pytest.mark.asyncio
    async def test_concurrent_provider_execution(self, enrichment_pipeline, sample_lead):
        """Should run Clearbit and LinkedIn concurrently."""
        import asyncio

        call_order = []

        async def slow_clearbit(*args, **kwargs):
            call_order.append("clearbit_start")
            await asyncio.sleep(0.1)
            call_order.append("clearbit_end")
            return EnrichmentResult(company_name="Acme Corp", source="clearbit")

        async def slow_linkedin(*args, **kwargs):
            call_order.append("linkedin_start")
            await asyncio.sleep(0.1)
            call_order.append("linkedin_end")
            return EnrichmentResult(person_title="VP Sales", source="linkedin")

        with patch.object(
            enrichment_pipeline._clearbit, "enrich", side_effect=slow_clearbit
        ), patch.object(
            enrichment_pipeline._linkedin, "enrich", side_effect=slow_linkedin
        ):
            result = await enrichment_pipeline.enrich(sample_lead)

        # Both should start before either finishes (concurrent execution)
        assert call_order[0] == "clearbit_start"
        assert call_order[1] == "linkedin_start"


# ---------- Public Data Provider Tests ----------


class TestPublicDataProvider:
    """Tests for the public data fallback provider."""

    @pytest.mark.asyncio
    async def test_enrich_from_domain(self):
        """Should extract basic company info from domain."""
        provider = PublicDataProvider()
        provider._client = AsyncMock()
        provider._client.get.return_value = MagicMock(
            status_code=200,
            text="<title>Acme Corp - Leading Automation Solutions</title>",
        )

        result = await provider.enrich("jane@acmecorp.com", "Acme Corp")

        assert result is not None
        assert result.source == "public"

    @pytest.mark.asyncio
    async def test_enrich_domain_unreachable(self):
        """Should return None when domain is unreachable."""
        provider = PublicDataProvider()
        provider._client = AsyncMock()
        provider._client.get.side_effect = ConnectionError("DNS resolution failed")

        result = await provider.enrich("user@nonexistent.xyz", "Unknown Corp")

        assert result is None


# ---------- Data Validation Tests ----------


class TestEnrichmentResult:
    """Tests for enrichment result data model."""

    def test_merge_results(self):
        """Should merge two enrichment results, preferring first."""
        clearbit_data = EnrichmentResult(
            company_name="Acme Corp",
            industry="Technology",
            employee_count=150,
            source="clearbit",
        )
        linkedin_data = EnrichmentResult(
            person_title="VP of Sales",
            person_seniority="executive",
            source="linkedin",
        )

        merged = clearbit_data.merge(linkedin_data)

        assert merged.company_name == "Acme Corp"
        assert merged.person_title == "VP of Sales"
        assert set(merged.sources) == {"clearbit", "linkedin"}

    def test_to_dict(self):
        """Should serialize to dict with all fields."""
        result = EnrichmentResult(
            company_name="Acme Corp",
            industry="Technology",
            employee_count=150,
            estimated_revenue="$10M-$50M",
            source="clearbit",
        )

        data = result.dict()

        assert data["company_name"] == "Acme Corp"
        assert data["industry"] == "Technology"
        assert data["employee_count"] == 150
        assert "source" in data

    def test_company_size_category(self):
        """Should correctly categorize company size."""
        small = EnrichmentResult(employee_count=10, source="test")
        medium = EnrichmentResult(employee_count=150, source="test")
        large = EnrichmentResult(employee_count=1500, source="test")
        enterprise = EnrichmentResult(employee_count=10000, source="test")

        assert small.company_size_category == "small"
        assert medium.company_size_category == "medium"
        assert large.company_size_category == "large"
        assert enterprise.company_size_category == "enterprise"
