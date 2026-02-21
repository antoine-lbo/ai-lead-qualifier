"""
AI-powered lead scoring engine combining rule-based and GPT-4 analysis.
Produces a 0-100 score with tier classification and actionable reasoning.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel

from src.api.services.enrichment import EnrichmentResult, CompanySize
from src.core.config import settings

logger = logging.getLogger(__name__)


# ── Models ─────────────────────────────────────────────────────────────────────


class LeadTier(str, Enum):
    HOT = "HOT"         # 80-100: Route to AE immediately
    WARM = "WARM"       # 50-79: Add to nurture sequence
    COLD = "COLD"       # 20-49: Add to marketing funnel
    DISQUALIFIED = "DISQUALIFIED"  # 0-19: Archive


class RoutingAction(str, Enum):
    ROUTE_TO_AE = "route_to_ae"
    ADD_TO_NURTURE = "add_to_nurture"
    ADD_TO_MARKETING = "add_to_marketing"
    ARCHIVE = "archive"
    MANUAL_REVIEW = "manual_review"


class ScoringBreakdown(BaseModel):
    company_fit: float = 0.0
    intent_signal: float = 0.0
    budget_indicator: float = 0.0
    urgency: float = 0.0
    details: dict[str, Any] = {}


class QualificationResult(BaseModel):
    score: int
    tier: LeadTier
    action: RoutingAction
    reasoning: str
    breakdown: ScoringBreakdown
    ai_analysis: str | None = None
    processing_time_ms: float = 0.0
    model_used: str = 'gpt-4'


@dataclass
class ICPConfig:
    """Ideal Customer Profile configuration."""
    target_industries: list[str] = field(default_factory=lambda: [
        'technology', 'finance', 'healthcare', 'e-commerce', 'saas',
    ])
    min_company_size: int = 50
    max_company_size: int = 10000
    min_revenue: float = 1_000_000
    target_countries: list[str] = field(default_factory=lambda: [
        'US', 'UK', 'CA', 'DE', 'FR', 'AU',
    ])
    high_value_titles: list[str] = field(default_factory=lambda: [
        'ceo', 'cto', 'cfo', 'vp', 'director', 'head of', 'chief',
    ])
    decision_maker_departments: list[str] = field(default_factory=lambda: [
        'engineering', 'product', 'operations', 'c-suite', 'executive',
    ])


@dataclass
class ScoringWeights:
    company_fit: float = 0.35
    intent_signal: float = 0.30
    budget_indicator: float = 0.20
    urgency: float = 0.15

# ── Scoring Engine ─────────────────────────────────────────────────────────────


class ScoringEngine:
    """Hybrid scoring engine: rule-based signals + GPT-4 analysis."""

    def __init__(
        self,
        icp: ICPConfig | None = None,
        weights: ScoringWeights | None = None,
        openai_client: AsyncOpenAI | None = None,
    ):
        self.icp = icp or ICPConfig()
        self.weights = weights or ScoringWeights()
        self._openai = openai_client or AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def qualify(
        self,
        email: str,
        message: str,
        enrichment: EnrichmentResult,
        company_override: str | None = None,
    ) -> QualificationResult:
        """Run full qualification pipeline: rules + AI analysis."""
        start = time.monotonic()

        # Step 1: Rule-based scoring
        breakdown = self._rule_based_scoring(enrichment, message)

        # Step 2: AI analysis for nuanced scoring
        ai_analysis = await self._ai_analysis(email, message, enrichment, breakdown)

        # Step 3: Combine scores
        raw_score = (
            breakdown.company_fit * self.weights.company_fit +
            breakdown.intent_signal * self.weights.intent_signal +
            breakdown.budget_indicator * self.weights.budget_indicator +
            breakdown.urgency * self.weights.urgency
        )

        # Apply AI adjustment (-10 to +10)
        ai_adjustment = ai_analysis.get('score_adjustment', 0)
        final_score = max(0, min(100, int(raw_score + ai_adjustment)))

        # Step 4: Classify
        tier = self._classify_tier(final_score)
        action = self._determine_action(tier, enrichment)

        processing_time = (time.monotonic() - start) * 1000

        return QualificationResult(
            score=final_score,
            tier=tier,
            action=action,
            reasoning=ai_analysis.get('reasoning', self._generate_reasoning(breakdown, tier)),
            breakdown=breakdown,
            ai_analysis=ai_analysis.get('detailed_analysis'),
            processing_time_ms=processing_time,
        )

    # ── Rule-Based Scoring ──────────────────────────────────────────────────

    def _rule_based_scoring(self, enrichment: EnrichmentResult, message: str) -> ScoringBreakdown:
        return ScoringBreakdown(
            company_fit=self._score_company_fit(enrichment),
            intent_signal=self._score_intent(message, enrichment),
            budget_indicator=self._score_budget(enrichment, message),
            urgency=self._score_urgency(message),
        )

    def _score_company_fit(self, enrichment: EnrichmentResult) -> float:
        """Score 0-100 based on ICP match."""
        score = 0.0
        co = enrichment.company
        person = enrichment.person

        # Industry match (0-30)
        if co.industry:
            if co.industry.lower() in [i.lower() for i in self.icp.target_industries]:
                score += 30
            elif any(kw in co.industry.lower() for kw in ['tech', 'software', 'digital']):
                score += 15

        # Company size (0-25)
        if co.employee_count:
            if self.icp.min_company_size <= co.employee_count <= self.icp.max_company_size:
                score += 25
            elif co.employee_count > self.icp.max_company_size:
                score += 15  # Enterprise, still valuable
            elif co.employee_count >= 10:
                score += 10

        # Revenue (0-20)
        if co.estimated_revenue:
            try:
                rev_str = co.estimated_revenue.replace('$', '').replace(',', '').lower()
                if 'm' in rev_str: rev = float(rev_str.replace('m', '')) * 1_000_000
                elif 'b' in rev_str: rev = float(rev_str.replace('b', '')) * 1_000_000_000
                else: rev = float(rev_str)
                if rev >= self.icp.min_revenue: score += 20
                elif rev >= 500_000: score += 10
            except (ValueError, AttributeError): pass

        # Decision maker (0-15)
        if person.title:
            title_lower = person.title.lower()
            if any(t in title_lower for t in self.icp.high_value_titles):
                score += 15
            elif any(d in title_lower for d in ['manager', 'lead', 'senior']):
                score += 8

        # Geography (0-10)
        if co.country and co.country in self.icp.target_countries:
            score += 10

        return min(100, score)
    def _score_intent(self, message: str, enrichment: EnrichmentResult) -> float:
        """Score 0-100 based on intent signals in the message."""
        score = 0.0
        msg = message.lower()

        # High-intent keywords (0-40)
        high_intent = ['demo', 'pricing', 'quote', 'purchase', 'buy', 'implement', 'integrate', 'migrate', 'replace', 'switch from']
        mid_intent = ['interested', 'looking for', 'need', 'want', 'solution', 'tool', 'platform', 'evaluate', 'compare']
        low_intent = ['learn', 'information', 'curious', 'exploring', 'research', 'what is']

        if any(kw in msg for kw in high_intent): score += 40
        elif any(kw in msg for kw in mid_intent): score += 25
        elif any(kw in msg for kw in low_intent): score += 10

        # Specificity signals (0-30)
        if any(w in msg for w in ['team of', 'employees', 'users', 'seats']): score += 15
        if any(w in msg for w in ['timeline', 'deadline', 'by q', 'this quarter', 'this month']): score += 15

        # Pain point indicators (0-20)
        pain_words = ['struggling', 'frustrated', 'problem', 'challenge', 'pain', 'slow', 'manual', 'inefficient', 'broken']
        if any(w in msg for w in pain_words): score += 20

        # Message length & quality (0-10)
        word_count = len(msg.split())
        if word_count > 50: score += 10
        elif word_count > 20: score += 5

        return min(100, score)

    def _score_budget(self, enrichment: EnrichmentResult, message: str) -> float:
        """Score 0-100 based on budget indicators."""
        score = 0.0
        msg = message.lower()

        # Explicit budget mentions (0-50)
        if any(w in msg for w in ['budget', '$', 'spend', 'invest', 'allocat']): score += 50

        # Company size as budget proxy (0-30)
        size_scores = {
            CompanySize.MEGA: 30, CompanySize.ENTERPRISE: 28,
            CompanySize.LARGE: 25, CompanySize.MID_MARKET: 20,
            CompanySize.MEDIUM: 12, CompanySize.SMALL: 5, CompanySize.SOLO: 2,
        }
        if enrichment.company.size:
            score += size_scores.get(enrichment.company.size, 0)

        # Funding as budget proxy (0-20)
        if enrichment.company.funding_total:
            if enrichment.company.funding_total > 50_000_000: score += 20
            elif enrichment.company.funding_total > 10_000_000: score += 15
            elif enrichment.company.funding_total > 1_000_000: score += 10

        return min(100, score)

    def _score_urgency(self, message: str) -> float:
        """Score 0-100 based on urgency signals."""
        score = 0.0
        msg = message.lower()

        urgent_words = ['asap', 'urgent', 'immediately', 'right away', 'today']
        timeline_words = ['this week', 'this month', 'this quarter', 'by end of', 'deadline']
        planning_words = ['next quarter', 'next year', 'planning', 'roadmap', 'eventually']

        if any(w in msg for w in urgent_words): score += 80
        elif any(w in msg for w in timeline_words): score += 50
        elif any(w in msg for w in planning_words): score += 20

        # Competitor mentions imply active evaluation
        if any(w in msg for w in ['competitor', 'alternative', 'vs', 'compared to', 'switching from']):
            score += 20

        return min(100, score)
    # ── AI Analysis ─────────────────────────────────────────────────────────

    async def _ai_analysis(
        self, email: str, message: str, enrichment: EnrichmentResult, breakdown: ScoringBreakdown
    ) -> dict:
        """Use GPT-4 to provide nuanced scoring adjustment and reasoning."""
        try:
            prompt = self._build_analysis_prompt(email, message, enrichment, breakdown)
            response = await self._openai.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a lead qualification expert. Analyze the lead and provide a JSON response with: score_adjustment (-10 to +10), reasoning (1-2 sentences), and detailed_analysis (paragraph)."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"AI analysis failed, using rule-based only: {e}")
            return {'score_adjustment': 0, 'reasoning': self._generate_reasoning(breakdown, None)}

    def _build_analysis_prompt(
        self, email: str, message: str, enrichment: EnrichmentResult, breakdown: ScoringBreakdown
    ) -> str:
        co = enrichment.company
        person = enrichment.person
        return f"""Analyze this inbound lead:

Email: {email}
Message: {message}

Enriched Data:
- Company: {co.name or 'Unknown'} ({co.industry or 'Unknown industry'})
- Size: {co.employee_count or 'Unknown'} employees
- Revenue: {co.estimated_revenue or 'Unknown'}
- Contact: {person.full_name or 'Unknown'}, {person.title or 'Unknown title'}
- Tech Stack: {', '.join(co.tech_stack[:5]) if co.tech_stack else 'Unknown'}

Rule-Based Scores:
- Company Fit: {breakdown.company_fit}/100
- Intent Signal: {breakdown.intent_signal}/100
- Budget Indicator: {breakdown.budget_indicator}/100
- Urgency: {breakdown.urgency}/100

Provide a JSON response with score_adjustment, reasoning, and detailed_analysis."""

    # ── Classification ──────────────────────────────────────────────────────

    @staticmethod
    def _classify_tier(score: int) -> LeadTier:
        if score >= 80: return LeadTier.HOT
        if score >= 50: return LeadTier.WARM
        if score >= 20: return LeadTier.COLD
        return LeadTier.DISQUALIFIED

    @staticmethod
    def _determine_action(tier: LeadTier, enrichment: EnrichmentResult) -> RoutingAction:
        tier_actions = {
            LeadTier.HOT: RoutingAction.ROUTE_TO_AE,
            LeadTier.WARM: RoutingAction.ADD_TO_NURTURE,
            LeadTier.COLD: RoutingAction.ADD_TO_MARKETING,
            LeadTier.DISQUALIFIED: RoutingAction.ARCHIVE,
        }
        # Override: low confidence enrichment gets manual review
        if enrichment.confidence < 0.3 and tier in (LeadTier.HOT, LeadTier.WARM):
            return RoutingAction.MANUAL_REVIEW
        return tier_actions[tier]

    @staticmethod
    def _generate_reasoning(breakdown: ScoringBreakdown, tier: LeadTier | None) -> str:
        parts = []
        if breakdown.company_fit >= 60: parts.append('strong company fit')
        elif breakdown.company_fit >= 30: parts.append('moderate company fit')
        if breakdown.intent_signal >= 50: parts.append('clear purchase intent')
        if breakdown.budget_indicator >= 40: parts.append('budget indicators present')
        if breakdown.urgency >= 50: parts.append('time-sensitive need')
        if not parts: parts.append('limited qualification signals')
        summary = ', '.join(parts).capitalize()
        if tier: summary += f'. Classified as {tier.value}.'
        return summary
