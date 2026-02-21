"""
Lead Router — Smart routing engine for qualified leads.
Routes leads to the right sales rep based on territory, expertise, capacity, and round-robin.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum

import httpx
from pydantic import BaseModel

from .config import settings

logger = logging.getLogger(__name__)


class RoutingAction(str, Enum):
    """Possible routing actions for qualified leads."""
    ROUTE_TO_AE = "route_to_ae"
    ADD_TO_NURTURE = "add_to_nurture"
    ADD_TO_MARKETING = "add_to_marketing"
    SCHEDULE_DEMO = "schedule_demo"
    ASSIGN_SDR = "assign_sdr"


class SalesRep(BaseModel):
    """Sales representative profile."""
    id: str
    name: str
    email: str
    territories: list[str] = []
    industries: list[str] = []
    max_capacity: int = 50
    current_leads: int = 0
    specialties: list[str] = []
    is_available: bool = True
    last_assigned: Optional[datetime] = None

    @property
    def capacity_ratio(self) -> float:
        """Current capacity utilization (0.0 to 1.0)."""
        if self.max_capacity == 0:
            return 1.0
        return self.current_leads / self.max_capacity

    @property
    def has_capacity(self) -> bool:
        return self.current_leads < self.max_capacity and self.is_available


class RoutingResult(BaseModel):
    """Result of lead routing decision."""
    action: RoutingAction
    assigned_to: Optional[SalesRep] = None
    reason: str
    confidence: float
    fallback_rep: Optional[SalesRep] = None
    notifications_sent: list[str] = []


@dataclass
class RoutingRule:
    """Individual routing rule with priority."""
    name: str
    priority: int
    condition: callable
    action: RoutingAction
    description: str = ""


class LeadRouter:
    """
    Smart lead routing engine.
    
    Routes qualified leads based on:
    - Territory matching (geography)
    - Industry expertise alignment
    - Current rep capacity and workload
    - Round-robin for equal distribution
    - Custom routing rules
    """

    def __init__(self):
        self._reps: list[SalesRep] = []
        self._rules: list[RoutingRule] = []
        self._round_robin_index: int = 0
        self._slack_client = httpx.AsyncClient()
        self._crm_client = httpx.AsyncClient()
        self._setup_default_rules()

    def register_rep(self, rep: SalesRep) -> None:
        """Register a sales rep for lead assignment."""
        self._reps.append(rep)
        logger.info(f"Registered rep: {rep.name} ({rep.email})")

    def add_rule(self, rule: RoutingRule) -> None:
        """Add a custom routing rule."""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    async def route(self, lead_data: dict, qualification: dict) -> RoutingResult:
        """
        Route a qualified lead to the appropriate destination.
        
        Args:
            lead_data: Enriched lead information
            qualification: AI qualification results (score, tier, reasoning)
        
        Returns:
            RoutingResult with assignment and notifications
        """
        tier = qualification.get("tier", "COLD")
        score = qualification.get("score", 0)
        action = self._determine_action(tier)

        # For cold leads, skip rep assignment
        if action == RoutingAction.ADD_TO_MARKETING:
            return RoutingResult(
                action=action,
                reason=f"Score {score}/100 — added to marketing nurture",
                confidence=0.95,
            )

        # Find best rep match
        best_rep = await self._find_best_rep(lead_data, qualification)
        fallback = self._get_fallback_rep(exclude=best_rep)

        if not best_rep:
            logger.warning("No available reps — routing to nurture queue")
            return RoutingResult(
                action=RoutingAction.ADD_TO_NURTURE,
                reason="No available reps with capacity",
                confidence=0.7,
            )

        # Send notifications
        notifications = await self._send_notifications(
            lead_data, qualification, best_rep, action
        )

        # Update CRM
        await self._update_crm(lead_data, best_rep, qualification)

        # Update rep workload
        best_rep.current_leads += 1
        best_rep.last_assigned = datetime.utcnow()

        return RoutingResult(
            action=action,
            assigned_to=best_rep,
            reason=self._build_routing_reason(lead_data, best_rep, qualification),
            confidence=0.9 if tier == "HOT" else 0.8,
            fallback_rep=fallback,
            notifications_sent=notifications,
        )

    async def _find_best_rep(self, lead_data: dict, qualification: dict) -> Optional[SalesRep]:
        """Find the best available rep using weighted scoring."""
        available = [r for r in self._reps if r.has_capacity]
        if not available:
            return None

        scored = []
        for rep in available:
            score = self._score_rep_match(rep, lead_data)
            scored.append((score, rep))

        scored.sort(key=lambda x: x[0], reverse=True)

        # If top scores are close, use round-robin among top candidates
        top_score = scored[0][0]
        top_candidates = [r for s, r in scored if s >= top_score * 0.85]

        if len(top_candidates) > 1:
            selected = top_candidates[self._round_robin_index % len(top_candidates)]
            self._round_robin_index += 1
            return selected

        return scored[0][1]

    def _score_rep_match(self, rep: SalesRep, lead_data: dict) -> float:
        """Score how well a rep matches a lead (0-100)."""
        score = 0.0

        # Territory match (30 points)
        lead_location = lead_data.get("location", "").lower()
        if any(t.lower() in lead_location for t in rep.territories):
            score += 30

        # Industry match (30 points)
        lead_industry = lead_data.get("industry", "").lower()
        if any(i.lower() in lead_industry for i in rep.industries):
            score += 30

        # Capacity bonus (20 points) — prefer reps with more bandwidth
        score += (1 - rep.capacity_ratio) * 20

        # Recency penalty (20 points) — distribute leads evenly
        if rep.last_assigned:
            hours_since = (datetime.utcnow() - rep.last_assigned).total_seconds() / 3600
            score += min(hours_since / 24, 1.0) * 20
        else:
            score += 20  # Never assigned = full bonus

        return score

    def _determine_action(self, tier: str) -> RoutingAction:
        """Map qualification tier to routing action."""
        mapping = {
            "HOT": RoutingAction.ROUTE_TO_AE,
            "WARM": RoutingAction.ADD_TO_NURTURE,
            "COLD": RoutingAction.ADD_TO_MARKETING,
        }
        return mapping.get(tier, RoutingAction.ADD_TO_MARKETING)

    def _get_fallback_rep(self, exclude: Optional[SalesRep] = None) -> Optional[SalesRep]:
        """Get a fallback rep in case the primary is unavailable."""
        available = [
            r for r in self._reps
            if r.has_capacity and r != exclude
        ]
        if not available:
            return None
        return min(available, key=lambda r: r.capacity_ratio)

    async def _send_notifications(self, lead_data: dict, qualification: dict,
                                   rep: SalesRep, action: RoutingAction) -> list[str]:
        """Send Slack notifications for routed leads."""
        notifications = []
        score = qualification.get("score", 0)
        tier = qualification.get("tier", "UNKNOWN")

        if not settings.slack_webhook_url:
            return notifications

        try:
            message = {
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f"New {tier} Lead Assigned"}
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Company:* {lead_data.get('company', 'N/A')}"},
                            {"type": "mrkdwn", "text": f"*Score:* {score}/100"},
                            {"type": "mrkdwn", "text": f"*Assigned to:* {rep.name}"},
                            {"type": "mrkdwn", "text": f"*Action:* {action.value}"},
                        ]
                    }
                ]
            }

            await self._slack_client.post(
                settings.slack_webhook_url,
                json=message,
                timeout=5.0
            )
            notifications.append(f"slack:{rep.email}")
            logger.info(f"Slack notification sent for lead -> {rep.name}")

        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")

        return notifications

    async def _update_crm(self, lead_data: dict, rep: SalesRep,
                           qualification: dict) -> None:
        """Update CRM with routing decision."""
        if not settings.hubspot_api_key:
            return

        try:
            payload = {
                "properties": {
                    "hubspot_owner_id": rep.id,
                    "lead_score": qualification.get("score", 0),
                    "lead_tier": qualification.get("tier", "COLD"),
                    "qualification_reasoning": qualification.get("reasoning", ""),
                    "lifecyclestage": "salesqualifiedlead"
                        if qualification.get("tier") == "HOT"
                        else "marketingqualifiedlead",
                }
            }

            contact_email = lead_data.get("email", "")
            await self._crm_client.patch(
                f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_email}",
                json=payload,
                headers={"Authorization": f"Bearer {settings.hubspot_api_key}"},
                params={"idProperty": "email"},
                timeout=10.0
            )
            logger.info(f"CRM updated: {contact_email} -> {rep.name}")

        except Exception as e:
            logger.error(f"CRM update failed: {e}")

    def _build_routing_reason(self, lead_data: dict, rep: SalesRep,
                               qualification: dict) -> str:
        """Build a human-readable routing explanation."""
        parts = [f"Score: {qualification.get('score', 0)}/100"]

        lead_industry = lead_data.get("industry", "").lower()
        if any(i.lower() in lead_industry for i in rep.industries):
            parts.append(f"Industry match ({lead_industry})")

        lead_location = lead_data.get("location", "").lower()
        if any(t.lower() in lead_location for t in rep.territories):
            parts.append(f"Territory match ({lead_location})")

        parts.append(f"Rep capacity: {rep.current_leads}/{rep.max_capacity}")

        return " | ".join(parts)

    def _setup_default_rules(self) -> None:
        """Initialize default routing rules."""
        self.add_rule(RoutingRule(
            name="enterprise_priority",
            priority=1,
            condition=lambda lead, qual: (
                lead.get("estimated_revenue", 0) > 50_000_000
                and qual.get("score", 0) >= 80
            ),
            action=RoutingAction.ROUTE_TO_AE,
            description="Enterprise accounts with high scores go directly to AE",
        ))

        self.add_rule(RoutingRule(
            name="demo_request",
            priority=2,
            condition=lambda lead, qual: (
                "demo" in lead.get("message", "").lower()
                and qual.get("score", 0) >= 50
            ),
            action=RoutingAction.SCHEDULE_DEMO,
            description="Demo requests with decent scores get auto-scheduled",
        ))


# Global router instance
router = LeadRouter()
