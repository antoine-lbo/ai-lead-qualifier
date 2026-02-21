"""
Tests for CRM integration module.

Covers HubSpot and Salesforce integrations, contact creation,
deal pipeline management, and webhook sync.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.crm import (
    CRMClient,
    HubSpotClient,
    SalesforceClient,
    CRMContact,
    CRMDeal,
    CRMSyncResult,
    DealStage,
)


# ─── Fixtures ────────────────────────────────────────────


@pytest.fixture
def qualified_lead():
    """Sample qualified lead data."""
    return {
        "email": "jane@acme.com",
        "name": "Jane Smith",
        "company": "Acme Corp",
        "score": 87,
        "tier": "HOT",
        "reasoning": "Enterprise company, clear pain point",
        "enrichment": {
            "company_size": "200-500",
            "industry": "Technology",
            "estimated_revenue": "$50M-$100M",
            "linkedin_url": "https://linkedin.com/in/janesmith",
        },
        "qualified_at": datetime.utcnow().isoformat(),
    }


@pytest.fixture
def hubspot_client():
    """Mock HubSpot client."""
    client = HubSpotClient(api_key="test_key")
    client._http = AsyncMock()
    return client


@pytest.fixture
def salesforce_client():
    """Mock Salesforce client."""
    client = SalesforceClient(
        instance_url="https://test.salesforce.com",
        access_token="test_token",
    )
    client._http = AsyncMock()
    return client

# ─── HubSpot Tests ───────────────────────────────────────


class TestHubSpotClient:
    """Tests for HubSpot CRM integration."""

    @pytest.mark.asyncio
    async def test_create_contact(self, hubspot_client, qualified_lead):
        """Should create a new contact in HubSpot."""
        hubspot_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=201,
                json=lambda: {"id": "hs_contact_123", "properties": {}},
            )
        )

        contact = await hubspot_client.create_contact(qualified_lead)

        assert contact.crm_id == "hs_contact_123"
        assert contact.email == qualified_lead["email"]
        hubspot_client._http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_contact_duplicate(self, hubspot_client, qualified_lead):
        """Should handle duplicate contact gracefully."""
        hubspot_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=409,
                json=lambda: {"message": "Contact already exists", "id": "hs_existing_456"},
            )
        )

        contact = await hubspot_client.create_contact(qualified_lead)

        assert contact.crm_id == "hs_existing_456"
        assert contact.was_existing is True

    @pytest.mark.asyncio
    async def test_create_deal(self, hubspot_client, qualified_lead):
        """Should create a deal linked to the contact."""
        hubspot_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=201,
                json=lambda: {
                    "id": "hs_deal_789",
                    "properties": {"dealstage": "qualifiedtobuy"},
                },
            )
        )

        deal = await hubspot_client.create_deal(
            contact_id="hs_contact_123",
            lead_data=qualified_lead,
        )

        assert deal.crm_id == "hs_deal_789"
        assert deal.stage == DealStage.QUALIFIED

    @pytest.mark.asyncio
    async def test_update_deal_stage(self, hubspot_client):
        """Should update deal stage in pipeline."""
        hubspot_client._http.patch = AsyncMock(
            return_value=MagicMock(status_code=200)
        )

        await hubspot_client.update_deal_stage(
            deal_id="hs_deal_789",
            stage=DealStage.DEMO_SCHEDULED,
        )

        hubspot_client._http.patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_qualification_notes(self, hubspot_client, qualified_lead):
        """Should add AI qualification notes to contact."""
        hubspot_client._http.post = AsyncMock(
            return_value=MagicMock(status_code=201)
        )

        await hubspot_client.add_note(
            contact_id="hs_contact_123",
            body=f"AI Score: {qualified_lead['score']} | {qualified_lead['reasoning']}",
        )

        hubspot_client._http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_rate_limit_handling(self, hubspot_client, qualified_lead):
        """Should handle HubSpot API rate limits with retry."""
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(status_code=429, headers={"Retry-After": "1"})
            return MagicMock(
                status_code=201,
                json=lambda: {"id": "hs_contact_retry"},
            )

        hubspot_client._http.post = mock_post
        contact = await hubspot_client.create_contact(qualified_lead)

        assert contact.crm_id == "hs_contact_retry"
        assert call_count == 2

class TestSalesforceClient:
    """Tests for Salesforce CRM client."""

    @pytest.mark.asyncio
    async def test_create_lead(self, salesforce_client, qualified_lead):
        """Should create a new lead in Salesforce."""
        salesforce_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=201,
                json=lambda: {"id": "00Q5f000003ABC", "success": True},
            )
        )

        contact = await salesforce_client.create_lead(qualified_lead)

        assert contact.crm_id == "00Q5f000003ABC"
        assert contact.email == qualified_lead["email"]
        salesforce_client._http.post.assert_called_once()
        call_args = salesforce_client._http.post.call_args
        assert "/sobjects/Lead" in str(call_args)

    @pytest.mark.asyncio
    async def test_convert_lead_to_contact(self, salesforce_client):
        """Should convert a qualified lead to a contact."""
        salesforce_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {
                    "accountId": "001ABC",
                    "contactId": "003ABC",
                    "opportunityId": "006ABC",
                    "success": True,
                },
            )
        )

        result = await salesforce_client.convert_lead(
            lead_id="00Q5f000003ABC",
            convert_to_opportunity=True,
        )

        assert result["contactId"] == "003ABC"
        assert result["opportunityId"] == "006ABC"
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_create_opportunity(self, salesforce_client, qualified_lead):
        """Should create an opportunity from qualified lead."""
        salesforce_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=201,
                json=lambda: {"id": "006ABC", "success": True},
            )
        )

        deal = await salesforce_client.create_opportunity(
            contact_id="003ABC",
            lead_data=qualified_lead,
        )

        assert deal.crm_id == "006ABC"
        assert deal.stage == DealStage.QUALIFIED
        call_args = salesforce_client._http.post.call_args
        payload = call_args[1].get("json", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert "Acme Corp" in str(call_args)

    @pytest.mark.asyncio
    async def test_update_opportunity_stage(self, salesforce_client):
        """Should update opportunity stage."""
        salesforce_client._http.patch = AsyncMock(
            return_value=MagicMock(status_code=204)
        )

        await salesforce_client.update_opportunity_stage(
            opportunity_id="006ABC",
            stage=DealStage.DEMO_SCHEDULED,
        )

        salesforce_client._http.patch.assert_called_once()
        call_args = salesforce_client._http.patch.call_args
        assert "006ABC" in str(call_args)

    @pytest.mark.asyncio
    async def test_api_auth_failure(self, salesforce_client, qualified_lead):
        """Should handle Salesforce auth token expiration."""
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(
                    status_code=401,
                    json=lambda: [{"errorCode": "INVALID_SESSION_ID"}],
                )
            return MagicMock(
                status_code=201,
                json=lambda: {"id": "00Q_refreshed", "success": True},
            )

        salesforce_client._http.post = mock_post
        salesforce_client._refresh_token = AsyncMock()

        contact = await salesforce_client.create_lead(qualified_lead)

        assert contact.crm_id == "00Q_refreshed"
        salesforce_client._refresh_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_bulk_upsert(self, salesforce_client):
        """Should handle bulk upsert of multiple leads."""
        leads = [
            {"email": f"lead{i}@company.com", "company": f"Company {i}"}
            for i in range(5)
        ]
        salesforce_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: [
                    {"id": f"00Q{i}", "success": True} for i in range(5)
                ],
            )
        )

        results = await salesforce_client.bulk_upsert(leads)

        assert len(results) == 5
        assert all(r["success"] for r in results)


class TestCRMSyncResult:
    """Tests for CRM sync result tracking."""

    @pytest.mark.asyncio
    async def test_full_sync_success(self, hubspot_client, qualified_lead):
        """Should track a complete successful sync."""
        hubspot_client._http.post = AsyncMock(
            return_value=MagicMock(
                status_code=201,
                json=lambda: {"id": "hs_sync_1"},
            )
        )
        hubspot_client._http.patch = AsyncMock(
            return_value=MagicMock(status_code=200)
        )

        result = CRMSyncResult()
        result.start()

        contact = await hubspot_client.create_contact(qualified_lead)
        result.add_success("contact", contact.crm_id)

        deal = await hubspot_client.create_deal(
            contact_id=contact.crm_id,
            lead_data=qualified_lead,
        )
        result.add_success("deal", deal.crm_id)
        result.complete()

        assert result.is_success is True
        assert result.total_synced == 2
        assert result.errors == []
        assert result.duration_ms > 0

    @pytest.mark.asyncio
    async def test_partial_sync_failure(self, hubspot_client, qualified_lead):
        """Should track partial failures in sync."""
        hubspot_client._http.post = AsyncMock(
            side_effect=[
                MagicMock(
                    status_code=201,
                    json=lambda: {"id": "hs_partial_1"},
                ),
                MagicMock(
                    status_code=500,
                    json=lambda: {"message": "Internal server error"},
                ),
            ]
        )

        result = CRMSyncResult()
        result.start()

        contact = await hubspot_client.create_contact(qualified_lead)
        result.add_success("contact", contact.crm_id)

        try:
            await hubspot_client.create_deal(
                contact_id=contact.crm_id,
                lead_data=qualified_lead,
            )
        except Exception as e:
            result.add_error("deal", str(e))

        result.complete()

        assert result.is_success is False
        assert result.total_synced == 1
        assert len(result.errors) == 1
        assert "deal" in result.errors[0]["entity"]


class TestFieldMapping:
    """Tests for CRM field mapping between providers."""

    def test_hubspot_field_mapping(self):
        """Should map internal fields to HubSpot properties."""
        lead_data = {
            "email": "test@acme.com",
            "company": "Acme Corp",
            "first_name": "John",
            "last_name": "Doe",
            "score": 87,
            "tier": "HOT",
        }

        mapped = HubSpotClient.map_fields(lead_data)

        assert mapped["properties"]["email"] == "test@acme.com"
        assert mapped["properties"]["company"] == "Acme Corp"
        assert mapped["properties"]["firstname"] == "John"
        assert mapped["properties"]["lastname"] == "Doe"
        assert mapped["properties"]["lead_score"] == 87
        assert mapped["properties"]["lead_status"] == "HOT"

    def test_salesforce_field_mapping(self):
        """Should map internal fields to Salesforce fields."""
        lead_data = {
            "email": "test@acme.com",
            "company": "Acme Corp",
            "first_name": "John",
            "last_name": "Doe",
            "score": 87,
            "tier": "HOT",
        }

        mapped = SalesforceClient.map_fields(lead_data)

        assert mapped["Email"] == "test@acme.com"
        assert mapped["Company"] == "Acme Corp"
        assert mapped["FirstName"] == "John"
        assert mapped["LastName"] == "Doe"
        assert mapped["Lead_Score__c"] == 87
        assert mapped["Lead_Status__c"] == "HOT"

    def test_missing_required_fields(self):
        """Should raise error for missing required fields."""
        lead_data = {"email": "test@acme.com"}  # missing company

        with pytest.raises(ValueError, match="company"):
            HubSpotClient.map_fields(lead_data)

    def test_custom_field_mapping(self):
        """Should support custom field mappings."""
        custom_mapping = {
            "email": "Email_Address__c",
            "company": "Organization__c",
            "score": "AI_Score__c",
        }
        lead_data = {
            "email": "test@acme.com",
            "company": "Acme Corp",
            "score": 87,
        }

        mapped = SalesforceClient.map_fields(
            lead_data, custom_mapping=custom_mapping
        )

        assert mapped["Email_Address__c"] == "test@acme.com"
        assert mapped["Organization__c"] == "Acme Corp"
        assert mapped["AI_Score__c"] == 87
