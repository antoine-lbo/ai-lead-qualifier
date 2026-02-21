"""
CRM Integration Module

Provides unified interface for syncing qualified leads to HubSpot and Salesforce.
Supports contact creation, deal/opportunity management, and activity logging.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from src.config import settings
from src.models import QualifiedLead

logger = logging.getLogger(__name__)


class CRMProvider(str, Enum):
    """Supported CRM platforms."""
    HUBSPOT = "hubspot"
    SALESFORCE = "salesforce"


class CRMContact(BaseModel):
    """Standardized contact representation across CRMs."""
    id: Optional[str] = None
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    title: Optional[str] = None
    source: Optional[str] = None
    lead_score: Optional[int] = None
    qualification_tier: Optional[str] = None
    properties: dict[str, Any] = Field(default_factory=dict)


class CRMDeal(BaseModel):
    """Standardized deal/opportunity representation."""
    id: Optional[str] = None
    contact_id: str
    name: str
    stage: str
    amount: Optional[float] = None
    close_date: Optional[str] = None
    pipeline: Optional[str] = None
    properties: dict[str, Any] = Field(default_factory=dict)


class SyncResult(BaseModel):
    """Result of a CRM sync operation."""
    success: bool
    provider: str
    contact_id: Optional[str] = None
    deal_id: Optional[str] = None
    action: str  # created, updated, skipped
    message: str
    synced_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BaseCRMClient(ABC):
    """Abstract base class for CRM integrations."""

    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            headers=self._build_headers(),
        )

    @abstractmethod
    def _build_headers(self) -> dict[str, str]:
        """Build authentication headers for the CRM API."""
        ...

    @abstractmethod
    async def find_contact(self, email: str) -> Optional[CRMContact]:
        """Search for an existing contact by email."""
        ...

    @abstractmethod
    async def create_contact(self, contact: CRMContact) -> str:
        """Create a new contact. Returns the CRM contact ID."""
        ...

    @abstractmethod
    async def update_contact(self, contact_id: str, properties: dict) -> None:
        """Update an existing contact with new properties."""
        ...

    @abstractmethod
    async def create_deal(self, deal: CRMDeal) -> str:
        """Create a new deal/opportunity. Returns the deal ID."""
        ...

    @abstractmethod
    async def log_activity(self, contact_id: str, note: str) -> None:
        """Log a note or activity on a contact record."""
        ...

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


# ─── HubSpot Integration ───────────────────────────────────────────


class HubSpotClient(BaseCRMClient):
    """HubSpot CRM integration using the v3 API."""

    STAGE_MAPPING = {
        "HOT": "qualifiedtobuy",
        "WARM": "presentationscheduled",
        "COLD": "appointmentscheduled",
    }

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            api_key=api_key or settings.HUBSPOT_API_KEY,
            base_url="https://api.hubapi.com",
        )
    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def find_contact(self, email: str) -> Optional[CRMContact]:
        """Search HubSpot for a contact by email."""
        try:
            resp = await self.client.post(
                "/crm/v3/objects/contacts/search",
                json={
                    "filterGroups": [{
                        "filters": [{
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }]
                    }],
                    "properties": [
                        "email", "firstname", "lastname", "company",
                        "phone", "jobtitle", "lead_score",
                    ],
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])

            if not results:
                return None

            contact_data = results[0]
            props = contact_data.get("properties", {})
            return CRMContact(
                id=contact_data["id"],
                email=props.get("email", email),
                first_name=props.get("firstname"),
                last_name=props.get("lastname"),
                company=props.get("company"),
                phone=props.get("phone"),
                title=props.get("jobtitle"),
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"HubSpot contact search failed: {e.response.status_code}")
            return None

    async def create_contact(self, contact: CRMContact) -> str:
        """Create a new contact in HubSpot."""
        properties = {
            "email": contact.email,
            "firstname": contact.first_name or "",
            "lastname": contact.last_name or "",
            "company": contact.company or "",
            "phone": contact.phone or "",
            "jobtitle": contact.title or "",
            "leadsource": contact.source or "ai_qualifier",
            **contact.properties,
        }

        if contact.lead_score is not None:
            properties["lead_score"] = str(contact.lead_score)
        if contact.qualification_tier:
            properties["qualification_tier"] = contact.qualification_tier

        resp = await self.client.post(
            "/crm/v3/objects/contacts",
            json={"properties": properties},
        )
        resp.raise_for_status()
        contact_id = resp.json()["id"]
        logger.info(f"Created HubSpot contact {contact_id} for {contact.email}")
        return contact_id

    async def update_contact(self, contact_id: str, properties: dict) -> None:
        """Update a HubSpot contact."""
        resp = await self.client.patch(
            f"/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )
        resp.raise_for_status()
        logger.info(f"Updated HubSpot contact {contact_id}")

    async def create_deal(self, deal: CRMDeal) -> str:
        """Create a deal in HubSpot and associate it with a contact."""
        properties = {
            "dealname": deal.name,
            "dealstage": self.STAGE_MAPPING.get(deal.stage, "appointmentscheduled"),
            "pipeline": deal.pipeline or "default",
            **deal.properties,
        }

        if deal.amount is not None:
            properties["amount"] = str(deal.amount)
        if deal.close_date:
            properties["closedate"] = deal.close_date

        resp = await self.client.post(
            "/crm/v3/objects/deals",
            json={"properties": properties},
        )
        resp.raise_for_status()
        deal_id = resp.json()["id"]

        # Associate deal with contact
        await self.client.put(
            f"/crm/v3/objects/deals/{deal_id}/associations/contacts/{deal.contact_id}/deal_to_contact",
        )
        logger.info(f"Created HubSpot deal {deal_id} for contact {deal.contact_id}")
        return deal_id

    async def log_activity(self, contact_id: str, note: str) -> None:
        """Create a note on a HubSpot contact."""
        resp = await self.client.post(
            "/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": note,
                    "hs_timestamp": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        resp.raise_for_status()
        note_id = resp.json()["id"]

        # Associate note with contact
        await self.client.put(
            f"/crm/v3/objects/notes/{note_id}/associations/contacts/{contact_id}/note_to_contact",
        )
        logger.info(f"Logged activity on HubSpot contact {contact_id}")

# ─── Salesforce Integration ────────────────────────────────────────


class SalesforceClient(BaseCRMClient):
    """Salesforce CRM integration using the REST API."""

    STAGE_MAPPING = {
        "HOT": "Qualification",
        "WARM": "Prospecting",
        "COLD": "Initial Contact",
    }

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        instance_url: Optional[str] = None,
    ):
        self._client_id = client_id or settings.SF_CLIENT_ID
        self._client_secret = client_secret or settings.SF_CLIENT_SECRET
        self._refresh_token = refresh_token or settings.SF_REFRESH_TOKEN
        self._access_token: Optional[str] = None
        instance = instance_url or settings.SF_INSTANCE_URL
        super().__init__(api_key="", base_url=instance)

    def _build_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
        }

    async def _ensure_token(self) -> None:
        """Refresh the OAuth access token if needed."""
        if self._access_token:
            return

        async with httpx.AsyncClient() as auth_client:
            resp = await auth_client.post(
                "https://login.salesforce.com/services/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
            )
            resp.raise_for_status()
            token_data = resp.json()
            self._access_token = token_data["access_token"]

            # Update instance URL if it changed
            new_instance = token_data.get("instance_url")
            if new_instance and new_instance != self.base_url:
                self.base_url = new_instance
                self.client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=30.0,
                )

        self.client.headers["Authorization"] = f"Bearer {self._access_token}"
        logger.info("Salesforce OAuth token refreshed")

    async def find_contact(self, email: str) -> Optional[CRMContact]:
        """Query Salesforce for a lead or contact by email."""
        await self._ensure_token()
        try:
            query = (
                f"SELECT Id, Email, FirstName, LastName, Company, Phone, Title "
                f"FROM Lead WHERE Email = '{email}' LIMIT 1"
            )
            resp = await self.client.get(
                "/services/data/v59.0/query",
                params={"q": query},
            )
            resp.raise_for_status()
            records = resp.json().get("records", [])

            if not records:
                return None

            record = records[0]
            return CRMContact(
                id=record["Id"],
                email=record.get("Email", email),
                first_name=record.get("FirstName"),
                last_name=record.get("LastName"),
                company=record.get("Company"),
                phone=record.get("Phone"),
                title=record.get("Title"),
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Salesforce query failed: {e.response.status_code}")
            return None

    async def create_contact(self, contact: CRMContact) -> str:
        """Create a new Lead in Salesforce."""
        await self._ensure_token()
        payload = {
            "Email": contact.email,
            "FirstName": contact.first_name or "",
            "LastName": contact.last_name or "Unknown",
            "Company": contact.company or "Unknown",
            "Phone": contact.phone or "",
            "Title": contact.title or "",
            "LeadSource": contact.source or "AI Qualifier",
            **contact.properties,
        }

        if contact.lead_score is not None:
            payload["Rating"] = (
                "Hot" if contact.lead_score >= 80
                else "Warm" if contact.lead_score >= 50
                else "Cold"
            )

        resp = await self.client.post(
            "/services/data/v59.0/sobjects/Lead",
            json=payload,
        )
        resp.raise_for_status()
        lead_id = resp.json()["id"]
        logger.info(f"Created Salesforce Lead {lead_id} for {contact.email}")
        return lead_id
    async def update_contact(self, contact_id: str, properties: dict) -> None:
        """Update a Salesforce Lead."""
        await self._ensure_token()
        resp = await self.client.patch(
            f"/services/data/v59.0/sobjects/Lead/{contact_id}",
            json=properties,
        )
        resp.raise_for_status()
        logger.info(f"Updated Salesforce Lead {contact_id}")

    async def create_deal(self, deal: CRMDeal) -> str:
        """Create an Opportunity in Salesforce."""
        await self._ensure_token()
        payload = {
            "Name": deal.name,
            "StageName": self.STAGE_MAPPING.get(deal.stage, "Prospecting"),
            "CloseDate": deal.close_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            **deal.properties,
        }

        if deal.amount is not None:
            payload["Amount"] = deal.amount

        resp = await self.client.post(
            "/services/data/v59.0/sobjects/Opportunity",
            json=payload,
        )
        resp.raise_for_status()
        opp_id = resp.json()["id"]
        logger.info(f"Created Salesforce Opportunity {opp_id}")
        return opp_id

    async def log_activity(self, contact_id: str, note: str) -> None:
        """Create a Task on a Salesforce Lead."""
        await self._ensure_token()
        resp = await self.client.post(
            "/services/data/v59.0/sobjects/Task",
            json={
                "WhoId": contact_id,
                "Subject": "AI Lead Qualification",
                "Description": note,
                "Status": "Completed",
                "Priority": "Normal",
                "ActivityDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            },
        )
        resp.raise_for_status()
        logger.info(f"Logged activity on Salesforce Lead {contact_id}")


# ─── CRM Manager (Orchestrator) ───────────────────────────────────


class CRMManager:
    """
    Orchestrates lead syncing across configured CRM providers.

    Handles contact deduplication, deal creation, and activity logging
    with automatic retries and error isolation per provider.
    """

    def __init__(self):
        self.clients: dict[CRMProvider, BaseCRMClient] = {}
        self._initialize_clients()

    def _initialize_clients(self) -> None:
        """Initialize CRM clients based on available configuration."""
        if getattr(settings, "HUBSPOT_API_KEY", None):
            self.clients[CRMProvider.HUBSPOT] = HubSpotClient()
            logger.info("HubSpot CRM client initialized")

        if getattr(settings, "SF_CLIENT_ID", None):
            self.clients[CRMProvider.SALESFORCE] = SalesforceClient()
            logger.info("Salesforce CRM client initialized")

        if not self.clients:
            logger.warning("No CRM providers configured")

    async def sync_qualified_lead(
        self,
        lead: QualifiedLead,
        providers: Optional[list[CRMProvider]] = None,
    ) -> list[SyncResult]:
        """
        Sync a qualified lead to one or more CRM providers.

        This is the main entry point. It handles:
        1. Contact deduplication (find or create)
        2. Score and tier updates
        3. Deal creation for hot leads
        4. Activity logging with AI reasoning
        """
        target_providers = providers or list(self.clients.keys())
        results = []

        tasks = [
            self._sync_to_provider(provider, lead)
            for provider in target_providers
            if provider in self.clients
        ]

        if not tasks:
            logger.warning(f"No active CRM clients for requested providers")
            return results

        settled = await asyncio.gather(*tasks, return_exceptions=True)

        for result in settled:
            if isinstance(result, Exception):
                logger.error(f"CRM sync error: {result}")
                results.append(SyncResult(
                    success=False,
                    provider="unknown",
                    action="error",
                    message=str(result),
                ))
            else:
                results.append(result)

        return results
    async def _sync_to_provider(
        self,
        provider: CRMProvider,
        lead: QualifiedLead,
    ) -> SyncResult:
        """Sync a single lead to a specific CRM provider."""
        client = self.clients[provider]

        try:
            # Step 1: Find or create contact
            existing = await client.find_contact(lead.email)

            if existing and existing.id:
                # Update existing contact with new qualification data
                await client.update_contact(existing.id, {
                    "lead_score": str(lead.score) if hasattr(lead, "score") else "",
                    "qualification_tier": lead.tier if hasattr(lead, "tier") else "",
                })
                contact_id = existing.id
                action = "updated"
            else:
                # Create new contact
                contact = CRMContact(
                    email=lead.email,
                    first_name=getattr(lead, "first_name", None),
                    last_name=getattr(lead, "last_name", None),
                    company=getattr(lead, "company", None),
                    lead_score=getattr(lead, "score", None),
                    qualification_tier=getattr(lead, "tier", None),
                    source="ai_qualifier",
                )
                contact_id = await client.create_contact(contact)
                action = "created"

            # Step 2: Create deal for hot leads
            deal_id = None
            tier = getattr(lead, "tier", "COLD")
            if tier == "HOT":
                deal = CRMDeal(
                    contact_id=contact_id,
                    name=f"AI Qualified: {getattr(lead, 'company', lead.email)}",
                    stage=tier,
                    amount=getattr(lead, "estimated_value", None),
                    properties={
                        "description": getattr(lead, "reasoning", "AI-qualified lead"),
                    },
                )
                deal_id = await client.create_deal(deal)

            # Step 3: Log qualification activity
            reasoning = getattr(lead, "reasoning", "No reasoning provided")
            note = (
                f"AI Lead Qualification Result\n"
                f"Score: {getattr(lead, 'score', 'N/A')}\n"
                f"Tier: {tier}\n"
                f"Reasoning: {reasoning}\n"
                f"Qualified at: {datetime.now(timezone.utc).isoformat()}"
            )
            await client.log_activity(contact_id, note)

            return SyncResult(
                success=True,
                provider=provider.value,
                contact_id=contact_id,
                deal_id=deal_id,
                action=action,
                message=f"Lead synced to {provider.value}: {action} contact, "
                        f"{"deal created" if deal_id else "no deal (not HOT)"}",
            )

        except Exception as e:
            logger.error(f"Failed to sync lead to {provider.value}: {e}")
            return SyncResult(
                success=False,
                provider=provider.value,
                action="error",
                message=f"Sync failed: {str(e)}",
            )

    async def close(self) -> None:
        """Close all CRM client connections."""
        for client in self.clients.values():
            await client.close()
        logger.info("All CRM clients closed")


# ─── Module-level convenience ──────────────────────────────────────


_manager: Optional[CRMManager] = None


def get_crm_manager() -> CRMManager:
    """Get or create the singleton CRM manager instance."""
    global _manager
    if _manager is None:
        _manager = CRMManager()
    return _manager


async def sync_lead_to_crm(
    lead: QualifiedLead,
    providers: Optional[list[CRMProvider]] = None,
) -> list[SyncResult]:
    """
    Convenience function to sync a qualified lead to configured CRMs.

    Usage:
        results = await sync_lead_to_crm(qualified_lead)
        for result in results:
            print(f"{result.provider}: {result.action} - {result.message}")
    """
    manager = get_crm_manager()
    return await manager.sync_qualified_lead(lead, providers)
