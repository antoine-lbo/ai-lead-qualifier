"""
AI Lead Qualification Engine

Uses GPT-4 to analyze and score inbound leads based on company fit,
intent signals, budget indicators, and urgency factors.
"""

import json
import logging
import time
from typing import Optional
from dataclasses import dataclass

import openai
from pydantic import BaseModel

from .config import settings, ScoringConfig
from .enrichment import EnrichmentService

logger = logging.getLogger(__name__)


class LeadInput(BaseModel):
    """Inbound lead data from webhook or API."""
    email: str
    company: Optional[str] = None
    name: Optional[str] = None
    message: Optional[str] = None
    source: Optional[str] = "website"
    page_url: Optional[str] = None
    utm_source: Optional[str] = None
    utm_campaign: Optional[str] = None


class QualificationResult(BaseModel):
    """Structured qualification output."""
    score: int
    tier: str  # HOT, WARM, COLD
    reasoning: str
    recommended_action: str
    enrichment: dict = {}
    signals: dict = {}
    processing_time_ms: int = 0


@dataclass
class ScoringWeights:
    """Configurable scoring weights."""
    company_fit: float = 0.35
    intent_signal: float = 0.30
    budget_indicator: float = 0.20
    urgency: float = 0.15

    def validate(self) -> bool:
        total = sum([self.company_fit, self.intent_signal,
                     self.budget_indicator, self.urgency])
        return abs(total - 1.0) < 0.01


class LeadQualifier:
    """
    Core qualification engine combining AI analysis with
    rule-based scoring for fast, accurate lead qualification.
    """

    def __init__(self, config: Optional[ScoringConfig] = None):
        self.config = config or ScoringConfig()
        self.weights = ScoringWeights(**self.config.weights)
        self.enrichment = EnrichmentService()
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

        if not self.weights.validate():
            raise ValueError("Scoring weights must sum to 1.0")

    async def qualify(self, lead: LeadInput) -> QualificationResult:
        """
        Main qualification pipeline:
        1. Enrich lead data (Clearbit, LinkedIn)
        2. Run AI analysis (GPT-4)
        3. Apply scoring rules
        4. Determine tier and action
        """
        start = time.time()

        # Step 1: Enrich lead with external data
        enrichment_data = await self.enrichment.enrich(
            email=lead.email, company=lead.company
        )

        # Step 2: AI-powered analysis
        ai_analysis = await self._analyze_with_gpt4(lead, enrichment_data)

        # Step 3: Calculate composite score
        score = self._calculate_score(ai_analysis, enrichment_data)

        # Step 4: Determine tier and action
        tier = self._determine_tier(score)
        action = self._determine_action(tier, enrichment_data)

        processing_time = int((time.time() - start) * 1000)

        result = QualificationResult(
            score=score,
            tier=tier,
            reasoning=ai_analysis.get("reasoning", ""),
            recommended_action=action,
            enrichment=enrichment_data,
            signals=ai_analysis.get("signals", {}),
            processing_time_ms=processing_time,
        )

        logger.info(
            f"Qualified lead {lead.email}: score={score}, "
            f"tier={tier}, time={processing_time}ms"
        )
        return result

    async def _analyze_with_gpt4(self, lead: LeadInput, enrichment: dict) -> dict:
        """Use GPT-4 to analyze lead quality and extract signals."""
        system_prompt = (
            "You are an expert B2B sales analyst. Analyze the inbound lead "
            "and provide a structured qualification assessment.\n\n"
            "Score each factor from 0-100:\n"
            "- company_fit: How well does the company match our ICP?\n"
            "- intent_signal: How strong is the buying intent?\n"
            "- budget_indicator: Are there signals of budget/authority?\n"
            "- urgency: How urgent is their need?\n\n"
            "Also extract key_signals, risk_factors, and reasoning.\n"
            "Respond in JSON format only."
        )

        icp = self.config.icp
        user_prompt = (
            f"Analyze this inbound lead:\n\n"
            f"Contact: {lead.name or 'Unknown'} ({lead.email})\n"
            f"Company: {lead.company or 'Unknown'}\n"
            f"Message: {lead.message or 'No message'}\n"
            f"Source: {lead.source}\n\n"
            f"Enrichment:\n"
            f"- Size: {enrichment.get('company_size', 'Unknown')}\n"
            f"- Industry: {enrichment.get('industry', 'Unknown')}\n"
            f"- Revenue: {enrichment.get('estimated_revenue', 'Unknown')}\n"
            f"- Tech: {enrichment.get('tech_stack', 'Unknown')}\n\n"
            f"ICP: size {icp.get('company_size', [50, 10000])}, "
            f"industries {icp.get('industries', [])}, "
            f"min revenue ${icp.get('min_revenue', 1000000):,}"
        )

        try:
            response = await self.client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"GPT-4 analysis failed: {e}")
            return self._fallback_analysis(lead, enrichment)

    def _fallback_analysis(self, lead: LeadInput, enrichment: dict) -> dict:
        """Rule-based fallback when AI analysis fails."""
        score = 50
        signals = []

        size = enrichment.get("employee_count", 0)
        if 50 <= size <= 10000:
            score += 10
            signals.append("Company size in ICP range")

        industry = enrichment.get("industry", "").lower()
        targets = [i.lower() for i in self.config.icp.get("industries", [])]
        if industry in targets:
            score += 15
            signals.append(f"Target industry: {industry}")

        if lead.message:
            msg = lead.message.lower()
            for kw in ["pricing", "demo", "trial", "budget", "timeline", "asap"]:
                if kw in msg:
                    score += 5
                    signals.append(f"High-intent keyword: {kw}")

        return {
            "company_fit": min(score, 100),
            "intent_signal": 50,
            "budget_indicator": 40,
            "urgency": 30,
            "signals": signals,
            "risk_factors": [],
            "reasoning": "Fallback rule-based analysis (AI unavailable)",
        }

    def _calculate_score(self, analysis: dict, enrichment: dict) -> int:
        """Calculate weighted composite score."""
        w = self.weights
        raw = (
            analysis.get("company_fit", 50) * w.company_fit
            + analysis.get("intent_signal", 50) * w.intent_signal
            + analysis.get("budget_indicator", 50) * w.budget_indicator
            + analysis.get("urgency", 50) * w.urgency
        )

        # Apply bonus multipliers
        multiplier = 1.0
        revenue = enrichment.get("estimated_revenue_value", 0)
        if revenue > 50_000_000:
            multiplier += 0.2
        elif revenue > 10_000_000:
            multiplier += 0.1

        if enrichment.get("source") in ("referral", "partner"):
            multiplier += 0.15

        return max(0, min(int(raw * multiplier), 100))

    def _determine_tier(self, score: int) -> str:
        """Map score to qualification tier."""
        if score >= 80:
            return "HOT"
        elif score >= 50:
            return "WARM"
        return "COLD"

    def _determine_action(self, tier: str, enrichment: dict) -> str:
        """Determine recommended action based on tier."""
        actions = {
            "HOT": "route_to_ae",
            "WARM": "add_to_nurture",
            "COLD": "add_to_marketing",
        }
        action = actions.get(tier, "add_to_marketing")

        # Enterprise override
        revenue = enrichment.get("estimated_revenue_value", 0)
        if revenue > 100_000_000 and tier in ("HOT", "WARM"):
            action = "route_to_enterprise_ae"

        return action


async def qualify_lead(lead_data: dict) -> QualificationResult:
    """Convenience function for qualifying a single lead."""
    qualifier = LeadQualifier()
    lead = LeadInput(**lead_data)
    return await qualifier.qualify(lead)
