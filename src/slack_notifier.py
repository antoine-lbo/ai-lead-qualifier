"""
Slack notification service for real-time lead alerts.
Sends rich Block Kit messages to configured channels.
"""

import logging
from typing import Optional

import httpx

from src.config import settings
from src.models import QualificationResult, LeadTier, LeadInput

logger = logging.getLogger(__name__)


TIER_EMOJI = {
    LeadTier.HOT: ":fire:",
    LeadTier.WARM: ":sunny:",
    LeadTier.COLD: ":snowflake:",
    LeadTier.DISQUALIFIED: ":no_entry_sign:",
}

TIER_COLOR = {
    LeadTier.HOT: "#FF4444",
    LeadTier.WARM: "#FFB84D",
    LeadTier.COLD: "#4DA6FF",
    LeadTier.DISQUALIFIED: "#999999",
}


class SlackNotifier:
    """Sends lead qualification alerts to Slack via Block Kit API."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        bot_token: Optional[str] = None,
        default_channel: str = "#sales-leads",
    ):
        self.webhook_url = webhook_url or settings.slack_webhook_url
        self.bot_token = bot_token or settings.slack_bot_token
        self.default_channel = default_channel
        self._client = httpx.AsyncClient(timeout=10.0)

    async def notify_new_lead(
        self,
        lead: LeadInput,
        result: QualificationResult,
        channel: Optional[str] = None,
    ) -> bool:
        """Send a rich notification for a newly qualified lead."""
        if result.tier == LeadTier.COLD and not settings.notify_cold_leads:
            logger.debug(f"Skipping Slack notification for cold lead {lead.email}")
            return False

        blocks = self._build_lead_blocks(lead, result)
        return await self._send_message(
            blocks=blocks,
            text=f"{TIER_EMOJI[result.tier]} New {result.tier.value} lead: {lead.company}",
            channel=channel,
        )

    async def notify_daily_summary(
        self,
        total: int,
        hot: int,
        warm: int,
        cold: int,
        avg_score: float,
        top_leads: list[tuple[str, int]],
    ) -> bool:
        """Send a daily lead summary to the team channel."""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":bar_chart: Daily Lead Report",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Total Leads:*\n{total}"},
                    {"type": "mrkdwn", "text": f"*Avg Score:*\n{avg_score:.1f}"},
                    {"type": "mrkdwn", "text": f"*:fire: Hot:*\n{hot}"},
                    {"type": "mrkdwn", "text": f"*:sunny: Warm:*\n{warm}"},
                    {"type": "mrkdwn", "text": f"*:snowflake: Cold:*\n{cold}"},
                ],
            },
        ]

        if top_leads:
            top_text = "\n".join(
                f"{i+1}. *{company}* — Score: {score}"
                for i, (company, score) in enumerate(top_leads[:5])
            )
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Top Leads Today:*\n{top_text}",
                },
            })

        return await self._send_message(
            blocks=blocks,
            text=f"Daily report: {total} leads processed, {hot} hot",
        )

    def _build_lead_blocks(
        self,
        lead: LeadInput,
        result: QualificationResult,
    ) -> list[dict]:
        """Build Slack Block Kit blocks for a lead notification."""
        emoji = TIER_EMOJI[result.tier]
        color = TIER_COLOR[result.tier]

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} New {result.tier.value} Lead — {lead.company}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Score:*\n{result.score}/100"},
                    {"type": "mrkdwn", "text": f"*Tier:*\n{result.tier.value}"},
                    {"type": "mrkdwn", "text": f"*Contact:*\n{lead.email}"},
                    {"type": "mrkdwn", "text": f"*Action:*\n{result.recommended_action.value}"},
                ],
            },
        ]

        # Add enrichment data if available
        if result.enrichment:
            enrichment_fields = []
            if result.enrichment.industry:
                enrichment_fields.append(
                    {"type": "mrkdwn", "text": f"*Industry:*\n{result.enrichment.industry}"}
                )
            if result.enrichment.company_size:
                enrichment_fields.append(
                    {"type": "mrkdwn", "text": f"*Size:*\n{result.enrichment.company_size}"}
                )
            if result.enrichment.estimated_revenue:
                enrichment_fields.append(
                    {"type": "mrkdwn", "text": f"*Revenue:*\n{result.enrichment.estimated_revenue}"}
                )
            if enrichment_fields:
                blocks.append({"type": "section", "fields": enrichment_fields})

        # Add AI reasoning
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*AI Analysis:*\n>{result.reasoning}",
            },
        })

        # Add action buttons for hot leads
        if result.tier == LeadTier.HOT:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":phone: Schedule Call"},
                        "style": "primary",
                        "action_id": f"schedule_call_{result.lead_id}",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":mag: View in CRM"},
                        "url": f"https://app.hubspot.com/contacts/search?q={lead.email}",
                        "action_id": f"view_crm_{result.lead_id}",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":x: Dismiss"},
                        "action_id": f"dismiss_{result.lead_id}",
                    },
                ],
            })

        return blocks

    async def _send_message(
        self,
        blocks: list[dict],
        text: str,
        channel: Optional[str] = None,
    ) -> bool:
        """Send a message to Slack via webhook or Bot API."""
        try:
            if self.webhook_url:
                response = await self._client.post(
                    self.webhook_url,
                    json={"blocks": blocks, "text": text},
                )
                response.raise_for_status()
                return True

            if self.bot_token:
                response = await self._client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                    json={
                        "channel": channel or self.default_channel,
                        "blocks": blocks,
                        "text": text,
                    },
                )
                data = response.json()
                if not data.get("ok"):
                    logger.error(f"Slack API error: {data.get('error')}")
                    return False
                return True

            logger.warning("No Slack credentials configured")
            return False

        except httpx.HTTPError as e:
            logger.error(f"Failed to send Slack notification: {e}")
            return False

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
