"""
AI Lead Qualifier — Python SDK Client

A typed, async-first Python client for the AI Lead Qualifier API.
Supports lead qualification, batch processing, and webhook management.

Usage:
    from lead_qualifier_client import LeadQualifierClient

    async with LeadQualifierClient(api_key="lq_...") as client:
        result = await client.qualify(
            email="john@acme.com",
            company="Acme Corp",
            message="Looking for automation solutions"
        )
        print(f"Score: {result.score}, Tier: {result.tier}")
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence
from urllib.parse import urljoin

import httpx


__version__ = "0.1.0"
__all__ = [
    "LeadQualifierClient",
    "QualificationResult",
    "BatchResult",
    "LeadTier",
    "Enrichment",
    "WebhookConfig",
    "APIError",
    "RateLimitError",
    "AuthenticationError",
]


# ─── Enums ────────────────────────────────────────────────────────

class LeadTier(str, Enum):
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


class DealStage(str, Enum):
    NEW = "new"
    QUALIFIED = "qualified"
    DEMO_SCHEDULED = "demo_scheduled"
    PROPOSAL_SENT = "proposal_sent"
    NEGOTIATION = "negotiation"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


# ─── Data Models ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Enrichment:
    """Company enrichment data returned with qualification."""
    company_size: Optional[str] = None
    industry: Optional[str] = None
    estimated_revenue: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    technologies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QualificationResult:
    """Result of a lead qualification request."""
    score: int
    tier: LeadTier
    reasoning: str
    recommended_action: str
    enrichment: Enrichment
    qualification_id: str
    processed_at: str
    processing_time_ms: int


@dataclass(frozen=True)
class BatchResult:
    """Result of a batch qualification request."""
    batch_id: str
    total: int
    processed: int
    results: list[QualificationResult]
    errors: list[dict[str, Any]]
    processing_time_ms: int


@dataclass
class WebhookConfig:
    """Webhook configuration for real-time notifications."""
    url: str
    events: list[str] = field(default_factory=lambda: ["qualification.completed"])
    secret: Optional[str] = None
    active: bool = True


# ─── Exceptions ───────────────────────────────────────────────────

class APIError(Exception):
    """Base exception for API errors."""

    def __init__(self, message: str, status_code: int, response: dict[str, Any] | None = None):
        self.message = message
        self.status_code = status_code
        self.response = response or {}
        super().__init__(f"[{status_code}] {message}")


class AuthenticationError(APIError):
    """Raised when API authentication fails."""
    pass


class RateLimitError(APIError):
    """Raised when API rate limit is exceeded."""

    def __init__(self, message: str, retry_after: int, **kwargs):
        self.retry_after = retry_after
        super().__init__(message, status_code=429, **kwargs)


class ValidationError(APIError):
    """Raised when request validation fails."""
    pass


# ─── Client ───────────────────────────────────────────────────────

class LeadQualifierClient:
    """
    Async Python client for the AI Lead Qualifier API.

    Args:
        api_key: Your API key (starts with "lq_")
        base_url: API base URL (default: http://localhost:8000)
        timeout: Request timeout in seconds (default: 30)
        max_retries: Maximum number of retries on failure (default: 3)
        retry_delay: Initial retry delay in seconds (default: 1.0)
    """

    DEFAULT_BASE_URL = "http://localhost:8000"
    API_PREFIX = "/api"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        if not api_key or not api_key.startswith("lq_"):
            raise ValueError("API key must start with 'lq_'")

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LeadQualifierClient:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"lead-qualifier-python/{__version__}",
            },
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError(
                "Client not initialized. Use `async with LeadQualifierClient(...) as client:`"
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Make an API request with retry logic."""
        url = f"{self.API_PREFIX}{path}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.request(method, url, **kwargs)

                if response.status_code == 401:
                    raise AuthenticationError(
                        "Invalid API key", status_code=401,
                        response=response.json(),
                    )

                if response.status_code == 422:
                    raise ValidationError(
                        "Validation failed", status_code=422,
                        response=response.json(),
                    )

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", "5"))
                    if attempt < self._max_retries:
                        await asyncio.sleep(retry_after)
                        continue
                    raise RateLimitError(
                        "Rate limit exceeded",
                        retry_after=retry_after,
                        response=response.json(),
                    )

                if response.status_code >= 500:
                    if attempt < self._max_retries:
                        delay = self._retry_delay * (2 ** attempt)
                        await asyncio.sleep(delay)
                        continue
                    raise APIError(
                        f"Server error: {response.status_code}",
                        status_code=response.status_code,
                        response=response.json() if response.content else None,
                    )

                response.raise_for_status()
                return response.json()

            except httpx.TimeoutException as e:
                last_error = e
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2 ** attempt))
                    continue

        raise APIError(
            f"Request failed after {self._max_retries + 1} attempts: {last_error}",
            status_code=0,
        )

    # ─── Lead Qualification ──────────────────────────────────────

    async def qualify(
        self,
        email: str,
        company: str,
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> QualificationResult:
        """
        Qualify a single lead.

        Args:
            email: Lead's email address
            company: Company name
            message: Optional message or context from the lead
            metadata: Optional additional metadata

        Returns:
            QualificationResult with score, tier, and enrichment data
        """
        payload: dict[str, Any] = {
            "email": email,
            "company": company,
        }
        if message:
            payload["message"] = message
        if metadata:
            payload["metadata"] = metadata

        data = await self._request("POST", "/qualify", json=payload)
        return self._parse_qualification(data)

    async def qualify_batch(
        self,
        leads: Sequence[dict[str, Any]],
        wait: bool = True,
        poll_interval: float = 2.0,
    ) -> BatchResult:
        """
        Qualify a batch of leads.

        Args:
            leads: List of lead dictionaries with email, company, message
            wait: If True, poll until batch is complete
            poll_interval: Seconds between status polls

        Returns:
            BatchResult with all qualification results
        """
        data = await self._request(
            "POST", "/qualify/batch", json={"leads": list(leads)}
        )

        if not wait:
            return self._parse_batch(data)

        batch_id = data["batch_id"]
        while data.get("status") != "completed":
            await asyncio.sleep(poll_interval)
            data = await self._request("GET", f"/qualify/batch/{batch_id}")

        return self._parse_batch(data)

    async def get_qualification(self, qualification_id: str) -> QualificationResult:
        """Retrieve a previous qualification result by ID."""
        data = await self._request("GET", f"/qualify/{qualification_id}")
        return self._parse_qualification(data)

    # ─── Webhooks ────────────────────────────────────────────────

    async def create_webhook(self, config: WebhookConfig) -> dict[str, Any]:
        """Register a new webhook endpoint."""
        return await self._request(
            "POST",
            "/webhooks",
            json={
                "url": config.url,
                "events": config.events,
                "secret": config.secret,
                "active": config.active,
            },
        )

    async def list_webhooks(self) -> list[dict[str, Any]]:
        """List all registered webhooks."""
        data = await self._request("GET", "/webhooks")
        return data.get("webhooks", [])

    async def delete_webhook(self, webhook_id: str) -> None:
        """Delete a webhook by ID."""
        await self._request("DELETE", f"/webhooks/{webhook_id}")

    # ─── Analytics ───────────────────────────────────────────────

    async def get_analytics(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """
        Get qualification analytics.

        Args:
            start_date: ISO date string (e.g., "2024-01-01")
            end_date: ISO date string

        Returns:
            Analytics data including conversion rates and tier distribution
        """
        params: dict[str, str] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        return await self._request("GET", "/analytics", params=params)

    async def health_check(self) -> dict[str, Any]:
        """Check API health status."""
        return await self._request("GET", "/health")

    # ─── Private Helpers ─────────────────────────────────────────

    @staticmethod
    def _parse_enrichment(data: dict[str, Any]) -> Enrichment:
        """Parse enrichment data from API response."""
        return Enrichment(
            company_size=data.get("company_size"),
            industry=data.get("industry"),
            estimated_revenue=data.get("estimated_revenue"),
            location=data.get("location"),
            website=data.get("website"),
            linkedin_url=data.get("linkedin_url"),
            technologies=data.get("technologies", []),
        )

    @classmethod
    def _parse_qualification(cls, data: dict[str, Any]) -> QualificationResult:
        """Parse a qualification result from API response."""
        return QualificationResult(
            score=data["score"],
            tier=LeadTier(data["tier"]),
            reasoning=data["reasoning"],
            recommended_action=data["recommended_action"],
            enrichment=cls._parse_enrichment(data.get("enrichment", {})),
            qualification_id=data.get("qualification_id", ""),
            processed_at=data.get("processed_at", ""),
            processing_time_ms=data.get("processing_time_ms", 0),
        )

    @classmethod
    def _parse_batch(cls, data: dict[str, Any]) -> BatchResult:
        """Parse a batch result from API response."""
        return BatchResult(
            batch_id=data["batch_id"],
            total=data["total"],
            processed=data["processed"],
            results=[
                cls._parse_qualification(r) for r in data.get("results", [])
            ],
            errors=data.get("errors", []),
            processing_time_ms=data.get("processing_time_ms", 0),
        )


# ─── Sync Wrapper ─────────────────────────────────────────────────

class LeadQualifierClientSync:
    """
    Synchronous wrapper around the async client.

    Usage:
        client = LeadQualifierClientSync(api_key="lq_...")
        result = client.qualify(email="john@acme.com", company="Acme Corp")
    """

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._client: LeadQualifierClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def __enter__(self):
        self._loop = asyncio.new_event_loop()
        self._client = LeadQualifierClient(**self._kwargs)
        self._loop.run_until_complete(self._client.__aenter__())
        return self

    def __exit__(self, *args):
        if self._client and self._loop:
            self._loop.run_until_complete(self._client.__aexit__(*args))
            self._loop.close()

    def qualify(self, **kwargs) -> QualificationResult:
        return self._loop.run_until_complete(self._client.qualify(**kwargs))

    def qualify_batch(self, **kwargs) -> BatchResult:
        return self._loop.run_until_complete(self._client.qualify_batch(**kwargs))

    def get_analytics(self, **kwargs) -> dict[str, Any]:
        return self._loop.run_until_complete(self._client.get_analytics(**kwargs))

    def health_check(self) -> dict[str, Any]:
        return self._loop.run_until_complete(self._client.health_check())
