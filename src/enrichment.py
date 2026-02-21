"""
Lead Enrichment Service

Pulls company and contact data from Clearbit, Hunter.io,
and LinkedIn (via Proxycurl) for lead qualification.
"""

import asyncio
import logging
from typing import Optional

import httpx
from pydantic import BaseModel

from .config import settings

logger = logging.getLogger(__name__)


class CompanyData(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    company_size: Optional[str] = None
    estimated_revenue: Optional[str] = None
    estimated_revenue_value: Optional[int] = None
    location: Optional[str] = None
    tech_stack: Optional[list] = None
    linkedin_url: Optional[str] = None
    founded_year: Optional[int] = None


class EnrichmentService:
    """Multi-source enrichment aggregating Clearbit + Hunter.io data."""

    def __init__(self):
        self.clearbit_key = settings.CLEARBIT_API_KEY
        self.hunter_key = settings.HUNTER_API_KEY
        self.client = httpx.AsyncClient(timeout=15.0)
        self._cache = {}

    async def enrich(self, email: str, company: Optional[str] = None) -> dict:
        """Enrich a lead from all sources in parallel."""
        cache_key = f"{email}:{company}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        tasks = [
            self._enrich_clearbit(email),
            self._verify_email_hunter(email),
        ]
        if company:
            domain = self._extract_domain(email)
            tasks.append(self._enrich_company_clearbit(domain))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        enrichment = {}
        for result in results:
            if isinstance(result, dict):
                enrichment.update(result)
            elif isinstance(result, Exception):
                logger.warning(f"Enrichment source failed: {result}")

        emp_count = enrichment.get("employee_count", 0)
        if emp_count:
            enrichment["company_size"] = self._classify_size(emp_count)
        if emp_count and not enrichment.get("estimated_revenue"):
            enrichment["estimated_revenue"] = self._estimate_revenue(emp_count)

        self._cache[cache_key] = enrichment
        return enrichment

    async def _enrich_clearbit(self, email: str) -> dict:
        """Fetch person + company data from Clearbit."""
        if not self.clearbit_key:
            return {}
        try:
            resp = await self.client.get(
                "https://person-stream.clearbit.com/v2/combined/find",
                params={"email": email},
                headers={"Authorization": f"Bearer {self.clearbit_key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                person = data.get("person", {})
                company = data.get("company", {})
                return {
                    "full_name": person.get("name", {}).get("fullName"),
                    "title": person.get("employment", {}).get("title"),
                    "seniority": person.get("employment", {}).get("seniority"),
                    "company_name": company.get("name"),
                    "industry": company.get("category", {}).get("industry"),
                    "employee_count": company.get("metrics", {}).get("employees"),
                    "tech_stack": company.get("tech", []),
                    "location": company.get("geo", {}).get("city"),
                }
            return {}
        except Exception as e:
            logger.error(f"Clearbit enrichment failed: {e}")
            return {}

    async def _enrich_company_clearbit(self, domain: str) -> dict:
        if not self.clearbit_key or not domain:
            return {}
        try:
            resp = await self.client.get(
                "https://company-stream.clearbit.com/v2/companies/find",
                params={"domain": domain},
                headers={"Authorization": f"Bearer {self.clearbit_key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"company_name": data.get("name"), "domain": data.get("domain")}
            return {}
        except Exception as e:
            logger.error(f"Company lookup failed: {e}")
            return {}

    async def _verify_email_hunter(self, email: str) -> dict:
        if not self.hunter_key:
            return {}
        try:
            resp = await self.client.get(
                "https://api.hunter.io/v2/email-verifier",
                params={"email": email, "api_key": self.hunter_key},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {"email_verified": data.get("result") == "deliverable"}
            return {}
        except Exception as e:
            logger.error(f"Hunter.io failed: {e}")
            return {}

    @staticmethod
    def _extract_domain(email: str) -> Optional[str]:
        try:
            return email.split("@")[1]
        except IndexError:
            return None

    @staticmethod
    def _classify_size(count: int) -> str:
        if count < 10: return "1-10"
        if count < 50: return "10-50"
        if count < 200: return "50-200"
        if count < 1000: return "200-1000"
        return "1000+"

    @staticmethod
    def _estimate_revenue(count: int) -> str:
        if count < 50: return "$1M-$10M"
        if count < 200: return "$10M-$50M"
        if count < 1000: return "$50M-$200M"
        return "$200M+"

    async def close(self):
        await self.client.aclose()
