"""
Pydantic models for the AI Lead Qualifier pipeline.
Handles validation, serialization, and type safety across the system.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, EmailStr, field_validator


class LeadTier(str, Enum):
    """Lead qualification tiers based on score thresholds."""
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"
    DISQUALIFIED = "DISQUALIFIED"


class LeadSource(str, Enum):
    """Inbound lead sources."""
    WEBSITE = "website"
    API = "api"
    CSV_UPLOAD = "csv_upload"
    HUBSPOT = "hubspot"
    SALESFORCE = "salesforce"
    MANUAL = "manual"


class RoutingAction(str, Enum):
    """Actions to take after qualification."""
    ROUTE_TO_AE = "route_to_ae"
    ADD_TO_NURTURE = "add_to_nurture"
    ADD_TO_MARKETING = "add_to_marketing"
    SCHEDULE_DEMO = "schedule_demo"
    DISQUALIFY = "disqualify"


class LeadInput(BaseModel):
    """Inbound lead data from webhook or API."""
    email: EmailStr
    company: str = Field(..., min_length=1, max_length=200)
    message: Optional[str] = Field(None, max_length=5000)
    name: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = None
    website: Optional[str] = None
    source: LeadSource = LeadSource.API
    metadata: Optional[dict] = None

    @field_validator("company")
    @classmethod
    def clean_company_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("website")
    @classmethod
    def normalize_website(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.startswith(("http://", "https://")):
            return f"https://{v}"
        return v


class EnrichmentData(BaseModel):
    """Company enrichment data from external sources."""
    company_size: Optional[str] = None
    industry: Optional[str] = None
    estimated_revenue: Optional[str] = None
    founded_year: Optional[int] = None
    headquarters: Optional[str] = None
    linkedin_url: Optional[str] = None
    description: Optional[str] = None
    technologies: list[str] = Field(default_factory=list)
    funding_total: Optional[float] = None
    employee_count: Optional[int] = None
    enrichment_source: Optional[str] = None
    enriched_at: Optional[datetime] = None


class ScoringBreakdown(BaseModel):
    """Detailed scoring breakdown by category."""
    company_fit: float = Field(..., ge=0, le=100)
    intent_signal: float = Field(..., ge=0, le=100)
    budget_indicator: float = Field(..., ge=0, le=100)
    urgency: float = Field(..., ge=0, le=100)

    @property
    def weighted_score(self) -> float:
        """Calculate weighted score using default weights."""
        weights = {
            "company_fit": 0.35,
            "intent_signal": 0.30,
            "budget_indicator": 0.20,
            "urgency": 0.15,
        }
        return (
            self.company_fit * weights["company_fit"]
            + self.intent_signal * weights["intent_signal"]
            + self.budget_indicator * weights["budget_indicator"]
            + self.urgency * weights["urgency"]
        )


class QualificationResult(BaseModel):
    """Complete qualification result returned by the pipeline."""
    lead_id: str
    score: int = Field(..., ge=0, le=100)
    tier: LeadTier
    reasoning: str
    recommended_action: RoutingAction
    breakdown: ScoringBreakdown
    enrichment: Optional[EnrichmentData] = None
    processing_time_ms: float
    model_version: str = "gpt-4-turbo"
    tokens_used: int = 0
    qualified_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_qualified(self) -> bool:
        return self.tier in (LeadTier.HOT, LeadTier.WARM)


class LeadResponse(BaseModel):
    """API response model for lead qualification."""
    score: int
    tier: str
    reasoning: str
    recommended_action: str
    enrichment: Optional[dict] = None

    @classmethod
    def from_result(cls, result: QualificationResult) -> "LeadResponse":
        enrichment_dict = None
        if result.enrichment:
            enrichment_dict = {
                "company_size": result.enrichment.company_size,
                "industry": result.enrichment.industry,
                "estimated_revenue": result.enrichment.estimated_revenue,
            }
        return cls(
            score=result.score,
            tier=result.tier.value,
            reasoning=result.reasoning,
            recommended_action=result.recommended_action.value,
            enrichment=enrichment_dict,
        )


class BatchInput(BaseModel):
    """Batch processing request for CSV uploads."""
    leads: list[LeadInput] = Field(..., min_length=1, max_length=10000)
    callback_url: Optional[str] = None
    priority: bool = False


class BatchStatus(BaseModel):
    """Status of a batch processing job."""
    batch_id: str
    total_leads: int
    processed: int
    qualified: int
    disqualified: int
    errors: int
    status: str = "processing"
    started_at: datetime
    completed_at: Optional[datetime] = None
    avg_processing_time_ms: Optional[float] = None

    @property
    def progress_pct(self) -> float:
        if self.total_leads == 0:
            return 0.0
        return round((self.processed / self.total_leads) * 100, 1)


class WebhookPayload(BaseModel):
    """Webhook notification payload sent to Slack or CRM."""
    event: str
    lead_id: str
    result: QualificationResult
    timestamp: datetime = Field(default_factory=datetime.utcnow)
