"""
Lead enrichment service that aggregates data from multiple providers.
Supports Clearbit, LinkedIn, and web scraping with caching and fallbacks.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

import httpx
import redis.asyncio as redis
from pydantic import BaseModel, EmailStr

from src.core.config import settings

logger = logging.getLogger(__name__)


# ── Data Models ────────────────────────────────────────────────────────────────


class EnrichmentSource(str, Enum):
    CLEARBIT = "clearbit"
    LINKEDIN = "linkedin"
    WEB_SCRAPE = "web_scrape"
    CACHE = "cache"


class CompanySize(str, Enum):
    SOLO = "1"
    SMALL = "2-10"
    MEDIUM = "11-50"
    MID_MARKET = "51-200"
    LARGE = "201-1000"
    ENTERPRISE = "1001-5000"
    MEGA = "5001+"


class EnrichedCompany(BaseModel):
    name: str | None = None
    domain: str | None = None
    industry: str | None = None
    size: CompanySize | None = None
    employee_count: int | None = None
    estimated_revenue: str | None = None
    founded_year: int | None = None
    description: str | None = None
    location: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    website: str | None = None
    tech_stack: list[str] = []
    funding_total: float | None = None
    tags: list[str] = []


class EnrichedPerson(BaseModel):
    full_name: str | None = None
    email: str | None = None
    title: str | None = None
    seniority: str | None = None
    department: str | None = None
    linkedin_url: str | None = None
    phone: str | None = None
    location: str | None = None
    bio: str | None = None


class EnrichmentResult(BaseModel):
    company: EnrichedCompany = EnrichedCompany()
    person: EnrichedPerson = EnrichedPerson()
    sources: list[EnrichmentSource] = []
    confidence: float = 0.0
    enriched_at: datetime = datetime.utcnow()
    cache_hit: bool = False
    errors: list[str] = []


@dataclass
class ProviderResult:
    source: EnrichmentSource
    company_data: dict = field(default_factory=dict)
    person_data: dict = field(default_factory=dict)
    success: bool = False
    error: str | None = None
    latency_ms: float = 0.0

# ── Enrichment Service ──────────────────────────────────────────────────────────


class EnrichmentService:
    """Multi-provider lead enrichment with caching and graceful fallbacks."""

    CACHE_TTL = timedelta(hours=24)
    REQUEST_TIMEOUT = 10.0

    def __init__(self, redis_client: redis.Redis | None = None):
        self._redis = redis_client
        self._http = httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT)
        self._providers = [
            self._enrich_clearbit,
            self._enrich_linkedin,
            self._enrich_web_scrape,
        ]

    async def enrich(self, email: str, company: str | None = None) -> EnrichmentResult:
        """
        Enrich a lead by email and optional company name.
        Checks cache first, then queries providers in parallel.
        """
        cache_key = self._cache_key(email, company)

        # Check cache
        cached = await self._get_cache(cache_key)
        if cached:
            cached.cache_hit = True
            return cached

        # Query all providers in parallel
        domain = email.split('@')[1] if '@' in email else None
        tasks = [provider(email, domain, company) for provider in self._providers]
        results: list[ProviderResult] = await asyncio.gather(*tasks, return_exceptions=False)

        # Merge results with priority ordering
        enrichment = self._merge_results(results)

        # Cache the result
        await self._set_cache(cache_key, enrichment)

        # Log enrichment metrics
        successful = [r for r in results if r.success]
        logger.info(
            f"Enriched {email}: {len(successful)}/{len(results)} providers succeeded, "
            f"confidence={enrichment.confidence:.2f}"
        )

        return enrichment

    # ── Provider: Clearbit ──────────────────────────────────────────────────────

    async def _enrich_clearbit(self, email: str, domain: str | None, company: str | None) -> ProviderResult:
        result = ProviderResult(source=EnrichmentSource.CLEARBIT)
        if not settings.CLEARBIT_API_KEY:
            result.error = "Clearbit API key not configured"
            return result

        try:
            import time
            start = time.monotonic()
            response = await self._http.get(
                f"https://person-stream.clearbit.com/v2/combined/find",
                params={"email": email},
                headers={"Authorization": f"Bearer {settings.CLEARBIT_API_KEY}"},
            )
            result.latency_ms = (time.monotonic() - start) * 1000

            if response.status_code == 200:
                data = response.json()
                person = data.get('person', {})
                co = data.get('company', {})

                result.person_data = {
                    'full_name': person.get('name', {}).get('fullName'),
                    'title': person.get('employment', {}).get('title'),
                    'seniority': person.get('employment', {}).get('seniority'),
                    'linkedin_url': person.get('linkedin', {}).get('handle'),
                    'location': person.get('location'),
                    'bio': person.get('bio'),
                }
                result.company_data = {
                    'name': co.get('name'),
                    'domain': co.get('domain'),
                    'industry': co.get('category', {}).get('industry'),
                    'employee_count': co.get('metrics', {}).get('employees'),
                    'estimated_revenue': co.get('metrics', {}).get('estimatedAnnualRevenue'),
                    'founded_year': co.get('foundedYear'),
                    'description': co.get('description'),
                    'location': co.get('location'),
                    'country': co.get('geo', {}).get('country'),
                    'tech_stack': co.get('tech', []),
                    'funding_total': co.get('metrics', {}).get('raised'),
                    'tags': co.get('tags', []),
                }
                result.success = True
            elif response.status_code == 404:
                result.error = "Not found in Clearbit"
            else:
                result.error = f"Clearbit HTTP {response.status_code}"

        except httpx.TimeoutException:
            result.error = "Clearbit request timed out"
        except Exception as e:
            result.error = f"Clearbit error: {str(e)}"
            logger.exception("Clearbit enrichment failed")

        return result
    # ── Provider: LinkedIn (via Proxycurl) ──────────────────────────────────────

    async def _enrich_linkedin(self, email: str, domain: str | None, company: str | None) -> ProviderResult:
        result = ProviderResult(source=EnrichmentSource.LINKEDIN)
        if not settings.PROXYCURL_API_KEY:
            result.error = "Proxycurl API key not configured"
            return result

        try:
            import time
            start = time.monotonic()

            # Resolve email to LinkedIn profile
            lookup_resp = await self._http.get(
                "https://nubela.co/proxycurl/api/linkedin/profile/resolve/email",
                params={"work_email": email},
                headers={"Authorization": f"Bearer {settings.PROXYCURL_API_KEY}"},
            )

            if lookup_resp.status_code != 200:
                result.error = f"LinkedIn lookup failed: HTTP {lookup_resp.status_code}"
                return result

            linkedin_url = lookup_resp.json().get('linkedin_profile_url')
            if not linkedin_url:
                result.error = "No LinkedIn profile found for email"
                return result

            # Fetch full profile
            profile_resp = await self._http.get(
                "https://nubela.co/proxycurl/api/v2/linkedin",
                params={"linkedin_profile_url": linkedin_url, "skills": "include"},
                headers={"Authorization": f"Bearer {settings.PROXYCURL_API_KEY}"},
            )
            result.latency_ms = (time.monotonic() - start) * 1000

            if profile_resp.status_code == 200:
                profile = profile_resp.json()
                experiences = profile.get('experiences', [])
                current_job = next((e for e in experiences if e.get('ends_at') is None), {})

                result.person_data = {
                    'full_name': profile.get('full_name'),
                    'title': current_job.get('title'),
                    'linkedin_url': linkedin_url,
                    'location': profile.get('city'),
                    'bio': profile.get('summary'),
                }
                if current_job.get('company'):
                    result.company_data = {
                        'name': current_job.get('company'),
                        'linkedin_url': current_job.get('company_linkedin_profile_url'),
                    }
                result.success = True

        except httpx.TimeoutException:
            result.error = "LinkedIn request timed out"
        except Exception as e:
            result.error = f"LinkedIn error: {str(e)}"
            logger.exception("LinkedIn enrichment failed")

        return result

    # ── Provider: Web Scrape ────────────────────────────────────────────────────

    async def _enrich_web_scrape(self, email: str, domain: str | None, company: str | None) -> ProviderResult:
        result = ProviderResult(source=EnrichmentSource.WEB_SCRAPE)
        if not domain:
            result.error = "No domain to scrape"
            return result

        try:
            import time
            start = time.monotonic()
            response = await self._http.get(
                f"https://{domain}",
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; LeadQualifier/1.0)"},
            )
            result.latency_ms = (time.monotonic() - start) * 1000

            if response.status_code == 200:
                html = response.text.lower()
                result.company_data = {
                    'domain': domain,
                    'website': str(response.url),
                }
                # Basic tech detection from HTML
                tech_signals = {
                    'react': 'react' in html or '__next' in html,
                    'vue': 'vue.js' in html or '__vue' in html,
                    'angular': 'ng-app' in html or 'angular' in html,
                    'wordpress': 'wp-content' in html,
                    'shopify': 'shopify' in html or 'cdn.shopify' in html,
                    'hubspot': 'hubspot' in html or 'hs-scripts' in html,
                    'intercom': 'intercom' in html,
                    'stripe': 'stripe' in html or 'js.stripe' in html,
                    'google_analytics': 'gtag' in html or 'google-analytics' in html,
                    'segment': 'segment.com' in html or 'analytics.js' in html,
                }
                result.company_data['tech_stack'] = [k for k, v in tech_signals.items() if v]
                result.success = True

        except Exception as e:
            result.error = f"Web scrape error: {str(e)}"

        return result
    # ── Result Merging ──────────────────────────────────────────────────────────

    def _merge_results(self, results: list[ProviderResult]) -> EnrichmentResult:
        """Merge multiple provider results with priority: Clearbit > LinkedIn > Web."""
        enrichment = EnrichmentResult()
        company_fields: dict[str, Any] = {}
        person_fields: dict[str, Any] = {}

        # Priority order: later providers fill gaps only
        for result in sorted(results, key=lambda r: r.success, reverse=True):
            if result.success:
                enrichment.sources.append(result.source)
                for k, v in result.company_data.items():
                    if v and k not in company_fields:
                        company_fields[k] = v
                for k, v in result.person_data.items():
                    if v and k not in person_fields:
                        person_fields[k] = v
            if result.error:
                enrichment.errors.append(f'{result.source.value}: {result.error}')

        # Build enriched models
        if company_fields:
            emp_count = company_fields.get('employee_count')
            if emp_count:
                company_fields['size'] = self._classify_size(emp_count)
            enrichment.company = EnrichedCompany(**{
                k: v for k, v in company_fields.items()
                if k in EnrichedCompany.model_fields
            })

        if person_fields:
            enrichment.person = EnrichedPerson(**{
                k: v for k, v in person_fields.items()
                if k in EnrichedPerson.model_fields
            })

        # Calculate confidence score
        enrichment.confidence = self._calculate_confidence(enrichment)
        enrichment.enriched_at = datetime.utcnow()

        return enrichment

    @staticmethod
    def _classify_size(employee_count: int) -> CompanySize:
        if employee_count <= 1: return CompanySize.SOLO
        if employee_count <= 10: return CompanySize.SMALL
        if employee_count <= 50: return CompanySize.MEDIUM
        if employee_count <= 200: return CompanySize.MID_MARKET
        if employee_count <= 1000: return CompanySize.LARGE
        if employee_count <= 5000: return CompanySize.ENTERPRISE
        return CompanySize.MEGA

    @staticmethod
    def _calculate_confidence(result: EnrichmentResult) -> float:
        """Score 0-1 based on data completeness across key fields."""
        weights = {
            'company_name': 0.15,
            'industry': 0.12,
            'employee_count': 0.12,
            'revenue': 0.10,
            'person_name': 0.12,
            'title': 0.12,
            'seniority': 0.08,
            'linkedin': 0.08,
            'tech_stack': 0.06,
            'location': 0.05,
        }
        score = 0.0
        if result.company.name: score += weights['company_name']
        if result.company.industry: score += weights['industry']
        if result.company.employee_count: score += weights['employee_count']
        if result.company.estimated_revenue: score += weights['revenue']
        if result.person.full_name: score += weights['person_name']
        if result.person.title: score += weights['title']
        if result.person.seniority: score += weights['seniority']
        if result.person.linkedin_url: score += weights['linkedin']
        if result.company.tech_stack: score += weights['tech_stack']
        if result.company.location or result.person.location: score += weights['location']
        return round(score, 3)

    # ── Caching ────────────────────────────────────────────────────────────────

    def _cache_key(self, email: str, company: str | None) -> str:
        raw = f"{email}:{company or ""}"
        return f"enrichment:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    async def _get_cache(self, key: str) -> EnrichmentResult | None:
        if not self._redis: return None
        try:
            data = await self._redis.get(key)
            if data:
                return EnrichmentResult.model_validate_json(data)
        except Exception:
            logger.warning(f"Cache read failed for {key}")
        return None

    async def _set_cache(self, key: str, result: EnrichmentResult) -> None:
        if not self._redis: return
        try:
            await self._redis.setex(key, int(self.CACHE_TTL.total_seconds()), result.model_dump_json())
        except Exception:
            logger.warning(f"Cache write failed for {key}")

    async def close(self) -> None:
        await self._http.aclose()
