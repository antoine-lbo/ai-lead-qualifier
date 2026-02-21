"""
Tests for the Lead Qualifier Python SDK Client.

Covers async client operations, error handling, retry logic,
batch processing, webhook management, and the sync wrapper.
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lead_qualifier_client import (
    APIError,
    AuthenticationError,
    BatchResult,
    DealStage,
    Enrichment,
    LeadQualifierClient,
    LeadQualifierClientSync,
    LeadTier,
    QualificationResult,
    RateLimitError,
    ValidationError,
    WebhookConfig,
)


# ─── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def api_key():
    return "lq_test_abc123def456"


@pytest.fixture
def base_url():
    return "https://api.test.local"


@pytest.fixture
def client(api_key, base_url):
    return LeadQualifierClient(api_key=api_key, base_url=base_url)


@pytest.fixture
def mock_qualify_response():
    return {
        "score": 87,
        "tier": "HOT",
        "reasoning": "Enterprise company with clear pain point",
        "recommended_action": "route_to_ae",
        "enrichment": {
            "company_size": "200-500",
            "industry": "Technology",
            "estimated_revenue": "$50M-$100M",
            "technologies": ["Python", "AWS", "Kubernetes"],
            "funding_stage": "Series C",
            "employee_count": 350,
        },
        "qualified_at": "2025-03-15T10:30:00Z",
    }


@pytest.fixture
def mock_batch_response():
    return {
        "batch_id": "batch_abc123",
        "status": "completed",
        "total": 3,
        "completed": 3,
        "failed": 0,
        "results": [
            {"score": 87, "tier": "HOT", "reasoning": "Great fit", "enrichment": {}},
            {"score": 45, "tier": "WARM", "reasoning": "Moderate fit", "enrichment": {}},
            {"score": 12, "tier": "COLD", "reasoning": "Poor fit", "enrichment": {}},
        ],
        "errors": [],
    }

# ─── Test Client Initialization ──────────────────────────────────


class TestClientInit:
    def test_default_base_url(self, api_key):
        client = LeadQualifierClient(api_key=api_key)
        assert client.base_url == "https://api.leadqualifier.com"

    def test_custom_base_url(self, api_key, base_url):
        client = LeadQualifierClient(api_key=api_key, base_url=base_url)
        assert client.base_url == base_url

    def test_missing_api_key_raises(self):
        with pytest.raises(ValueError, match="api_key is required"):
            LeadQualifierClient(api_key="")

    def test_custom_timeout(self, api_key):
        client = LeadQualifierClient(api_key=api_key, timeout=60.0)
        assert client.timeout == 60.0

    def test_custom_max_retries(self, api_key):
        client = LeadQualifierClient(api_key=api_key, max_retries=5)
        assert client.max_retries == 5


# ─── Test Async Context Manager ──────────────────────────────────


class TestAsyncContextManager:
    @pytest.mark.asyncio
    async def test_context_manager_creates_client(self, api_key, base_url):
        async with LeadQualifierClient(
            api_key=api_key, base_url=base_url
        ) as client:
            assert client._client is not None

    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self, api_key, base_url):
        client = LeadQualifierClient(api_key=api_key, base_url=base_url)
        async with client:
            http = client._http
        assert http.is_closed


# ─── Test Qualify ────────────────────────────────────────────────


class TestQualify:
    @pytest.mark.asyncio
    async def test_qualify_single_lead(self, client, mock_qualify_response):
        mock_response = httpx.Response(
            200,
            json=mock_qualify_response,
            request=httpx.Request("POST", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await client.qualify(
                email="john@acme.com",
                company="Acme Corp",
                message="Need automation for 50-person team",
            )

        assert isinstance(result, QualificationResult)
        assert result.score == 87
        assert result.tier == LeadTier.HOT
        assert result.enrichment.industry == "Technology"
        assert result.enrichment.employee_count == 350

    @pytest.mark.asyncio
    async def test_qualify_with_custom_fields(self, client, mock_qualify_response):
        mock_response = httpx.Response(
            200,
            json=mock_qualify_response,
            request=httpx.Request("POST", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_response
        ) as mock_req:
            await client.qualify(
                email="john@acme.com",
                company="Acme Corp",
                phone="+1-555-0123",
                website="https://acme.com",
                source="website_form",
            )

            call_args = mock_req.call_args
            assert call_args is not None

    @pytest.mark.asyncio
    async def test_qualify_validation_error(self, client):
        mock_response = httpx.Response(
            422,
            json={"detail": [{"field": "email", "msg": "invalid email"}]},
            request=httpx.Request("POST", "https://test.local"),
        )

        with patch.object(
            client, "_http", new_callable=lambda: MagicMock()
        ) as mock_http:
            mock_http.request = AsyncMock(return_value=mock_response)
            with pytest.raises(ValidationError):
                await client.qualify(email="invalid", company="Test")

# ─── Test Error Handling ─────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_authentication_error(self, client):
        mock_response = httpx.Response(
            401,
            json={"error": "Invalid API key"},
            request=httpx.Request("POST", "https://test.local"),
        )

        with patch.object(
            client, "_http", new_callable=lambda: MagicMock()
        ) as mock_http:
            mock_http.request = AsyncMock(return_value=mock_response)
            with pytest.raises(AuthenticationError) as exc_info:
                await client.qualify(email="test@test.com", company="Test")

            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_rate_limit_error(self, client):
        mock_response = httpx.Response(
            429,
            json={"error": "Rate limit exceeded"},
            headers={"Retry-After": "30"},
            request=httpx.Request("POST", "https://test.local"),
        )

        client._max_retries = 0  # Disable retries for this test

        with patch.object(
            client, "_http", new_callable=lambda: MagicMock()
        ) as mock_http:
            mock_http.request = AsyncMock(return_value=mock_response)
            with pytest.raises(RateLimitError) as exc_info:
                await client.qualify(email="test@test.com", company="Test")

            assert exc_info.value.retry_after == 30

    @pytest.mark.asyncio
    async def test_server_error_with_retry(self, client, mock_qualify_response):
        error_response = httpx.Response(
            500,
            json={"error": "Internal server error"},
            request=httpx.Request("POST", "https://test.local"),
        )
        success_response = httpx.Response(
            200,
            json=mock_qualify_response,
            request=httpx.Request("POST", "https://test.local"),
        )

        with patch.object(
            client, "_http", new_callable=lambda: MagicMock()
        ) as mock_http:
            mock_http.request = AsyncMock(
                side_effect=[error_response, success_response]
            )
            result = await client.qualify(
                email="test@test.com", company="Test"
            )

            assert result.score == 87
            assert mock_http.request.call_count == 2

    @pytest.mark.asyncio
    async def test_generic_api_error(self, client):
        mock_response = httpx.Response(
            503,
            json={"error": "Service unavailable"},
            request=httpx.Request("POST", "https://test.local"),
        )

        client._max_retries = 0

        with patch.object(
            client, "_http", new_callable=lambda: MagicMock()
        ) as mock_http:
            mock_http.request = AsyncMock(return_value=mock_response)
            with pytest.raises(APIError) as exc_info:
                await client.qualify(email="test@test.com", company="Test")

            assert exc_info.value.status_code == 503


# ─── Test Batch Processing ───────────────────────────────────────


class TestBatchProcessing:
    @pytest.mark.asyncio
    async def test_batch_qualify(self, client, mock_batch_response):
        # First call creates batch, second polls for result
        create_response = httpx.Response(
            202,
            json={"batch_id": "batch_abc123", "status": "processing"},
            request=httpx.Request("POST", "https://test.local"),
        )
        poll_response = httpx.Response(
            200,
            json=mock_batch_response,
            request=httpx.Request("GET", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock,
            side_effect=[create_response, poll_response]
        ):
            leads = [
                {"email": "a@test.com", "company": "A Corp"},
                {"email": "b@test.com", "company": "B Corp"},
                {"email": "c@test.com", "company": "C Corp"},
            ]
            result = await client.qualify_batch(leads=leads, wait=True)

        assert isinstance(result, BatchResult)
        assert result.batch_id == "batch_abc123"
        assert len(result.results) == 3
        assert result.results[0].tier == LeadTier.HOT
        assert result.results[2].tier == LeadTier.COLD

    @pytest.mark.asyncio
    async def test_batch_no_wait(self, client):
        create_response = httpx.Response(
            202,
            json={"batch_id": "batch_xyz789", "status": "processing"},
            request=httpx.Request("POST", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock,
            return_value=create_response
        ):
            result = await client.qualify_batch(
                leads=[{"email": "a@test.com", "company": "A"}],
                wait=False,
            )

        assert isinstance(result, BatchResult)
        assert result.batch_id == "batch_xyz789"

# ─── Test Webhooks ───────────────────────────────────────────────


class TestWebhooks:
    @pytest.mark.asyncio
    async def test_create_webhook(self, client):
        mock_response = httpx.Response(
            201,
            json={
                "id": "wh_abc123",
                "url": "https://myapp.com/webhooks",
                "events": ["lead.qualified", "lead.scored"],
                "secret": "whsec_test123",
                "is_active": True,
            },
            request=httpx.Request("POST", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await client.create_webhook(
                url="https://myapp.com/webhooks",
                events=["lead.qualified", "lead.scored"],
            )

        assert isinstance(result, WebhookConfig)
        assert result.url == "https://myapp.com/webhooks"
        assert "lead.qualified" in result.events

    @pytest.mark.asyncio
    async def test_list_webhooks(self, client):
        mock_response = httpx.Response(
            200,
            json=[
                {"id": "wh_1", "url": "https://a.com/wh", "events": ["lead.qualified"], "is_active": True},
                {"id": "wh_2", "url": "https://b.com/wh", "events": ["batch.completed"], "is_active": False},
            ],
            request=httpx.Request("GET", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_response
        ):
            webhooks = await client.list_webhooks()

        assert len(webhooks) == 2
        assert webhooks[0]["url"] == "https://a.com/wh"

    @pytest.mark.asyncio
    async def test_delete_webhook(self, client):
        mock_response = httpx.Response(
            204,
            request=httpx.Request("DELETE", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_response
        ):
            await client.delete_webhook("wh_abc123")


# ─── Test Analytics & Health ─────────────────────────────────────


class TestAnalyticsAndHealth:
    @pytest.mark.asyncio
    async def test_get_analytics(self, client):
        mock_response = httpx.Response(
            200,
            json={
                "total_leads": 1250,
                "qualified_leads": 890,
                "conversion_rate": 0.712,
                "avg_score": 62.5,
                "tier_distribution": {"HOT": 180, "WARM": 450, "COLD": 620},
                "avg_processing_time_ms": 2400,
            },
            request=httpx.Request("GET", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_response
        ):
            analytics = await client.get_analytics(period="30d")

        assert analytics["total_leads"] == 1250
        assert analytics["conversion_rate"] == 0.712

    @pytest.mark.asyncio
    async def test_health_check(self, client):
        mock_response = httpx.Response(
            200,
            json={
                "status": "healthy",
                "version": "1.2.0",
                "uptime_seconds": 86400,
                "database": "connected",
                "redis": "connected",
            },
            request=httpx.Request("GET", "https://test.local"),
        )

        with patch.object(
            client, "_request", new_callable=AsyncMock, return_value=mock_response
        ):
            health = await client.health_check()

        assert health["status"] == "healthy"
        assert health["database"] == "connected"


# ─── Test Data Models ────────────────────────────────────────────


class TestDataModels:
    def test_lead_tier_values(self):
        assert LeadTier.HOT.value == "HOT"
        assert LeadTier.WARM.value == "WARM"
        assert LeadTier.COLD.value == "COLD"

    def test_deal_stage_values(self):
        assert DealStage.PROSPECTING.value == "prospecting"
        assert DealStage.CLOSED_WON.value == "closed_won"
        assert DealStage.CLOSED_LOST.value == "closed_lost"

    def test_enrichment_frozen(self):
        enrichment = Enrichment(
            company_size="100-200",
            industry="Tech",
            estimated_revenue="$10M",
        )
        with pytest.raises(AttributeError):
            enrichment.company_size = "500-1000"

    def test_qualification_result_properties(self):
        enrichment = Enrichment(company_size="50-200", industry="Finance")
        result = QualificationResult(
            score=75,
            tier=LeadTier.WARM,
            reasoning="Good fit with moderate intent",
            recommended_action="add_to_nurture",
            enrichment=enrichment,
        )
        assert result.score == 75
        assert result.tier == LeadTier.WARM
        assert result.enrichment.industry == "Finance"


# ─── Test Sync Wrapper ───────────────────────────────────────────


class TestSyncWrapper:
    def test_sync_client_creates(self, api_key, base_url):
        with LeadQualifierClientSync(
            api_key=api_key, base_url=base_url
        ) as client:
            assert client._client is not None

    def test_sync_qualify(self, api_key, base_url, mock_qualify_response):
        with LeadQualifierClientSync(
            api_key=api_key, base_url=base_url
        ) as client:
            mock_response = httpx.Response(
                200,
                json=mock_qualify_response,
                request=httpx.Request("POST", "https://test.local"),
            )

            with patch.object(
                client._client, "_request",
                new_callable=AsyncMock,
                return_value=mock_response,
            ):
                result = client.qualify(
                    email="test@test.com", company="Test Corp"
                )

            assert isinstance(result, QualificationResult)
            assert result.score == 87

    def test_sync_health_check(self, api_key, base_url):
        with LeadQualifierClientSync(
            api_key=api_key, base_url=base_url
        ) as client:
            mock_response = httpx.Response(
                200,
                json={"status": "healthy", "version": "1.0.0"},
                request=httpx.Request("GET", "https://test.local"),
            )

            with patch.object(
                client._client, "_request",
                new_callable=AsyncMock,
                return_value=mock_response,
            ):
                health = client.health_check()

            assert health["status"] == "healthy"
