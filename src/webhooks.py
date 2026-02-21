"""
Webhook handlers for receiving leads from external sources.
Supports HubSpot, Typeform, Calendly, and custom webhooks.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from .config import settings
from .models import LeadInput, LeadSource
from .qualifier import LeadQualifier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
qualifier = LeadQualifier()


class WebhookResponse(BaseModel):
    status: str
    lead_id: Optional[str] = None
    score: Optional[int] = None
    message: str


def verify_signature(
    payload: bytes, signature: str, secret: str, algorithm: str = "sha256"
) -> bool:
    """Verify webhook signature using HMAC."""
    expected = hmac.new(
        secret.encode(), payload, getattr(hashlib, algorithm)
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/hubspot", response_model=WebhookResponse)
async def hubspot_webhook(
    request: Request,
    x_hubspot_signature: Optional[str] = Header(None),
):
    """
    Handle incoming leads from HubSpot form submissions.
    Validates signature if HUBSPOT_WEBHOOK_SECRET is configured.
    """
    body = await request.body()

    if settings.hubspot_webhook_secret and x_hubspot_signature:
        if not verify_signature(body, x_hubspot_signature, settings.hubspot_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(body)
    properties = data.get("properties", {})

    lead = LeadInput(
        email=properties.get("email", {}).get("value", ""),
        company=properties.get("company", {}).get("value", ""),
        name=properties.get("firstname", {}).get("value", ""),
        message=properties.get("message", {}).get("value", ""),
        source=LeadSource.HUBSPOT,
        metadata={
            "hubspot_vid": data.get("vid"),
            "form_id": data.get("form_id"),
            "page_url": properties.get("hs_latest_source_data_1", {}).get("value"),
        },
    )

    result = await qualifier.qualify(lead)
    logger.info(f"HubSpot lead qualified: {lead.email} -> {result.tier} ({result.score})")

    return WebhookResponse(
        status="qualified",
        lead_id=result.lead_id,
        score=result.score,
        message=f"Lead qualified as {result.tier}",
    )


@router.post("/typeform", response_model=WebhookResponse)
async def typeform_webhook(
    request: Request,
    typeform_signature: Optional[str] = Header(None, alias="Typeform-Signature"),
):
    """Handle leads from Typeform survey responses."""
    body = await request.body()

    if settings.typeform_webhook_secret and typeform_signature:
        sig = typeform_signature.replace("sha256=", "")
        if not verify_signature(body, sig, settings.typeform_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(body)
    answers = {a["field"]["ref"]: a for a in data.get("form_response", {}).get("answers", [])}

    def get_answer(ref: str) -> str:
        answer = answers.get(ref, {})
        return (
            answer.get("text", "")
            or answer.get("email", "")
            or answer.get("choice", {}).get("label", "")
            or str(answer.get("number", ""))
        )

    lead = LeadInput(
        email=get_answer("email"),
        company=get_answer("company"),
        name=get_answer("name"),
        message=get_answer("needs"),
        source=LeadSource.TYPEFORM,
        metadata={
            "form_id": data.get("form_response", {}).get("form_id"),
            "response_id": data.get("form_response", {}).get("token"),
            "submitted_at": data.get("form_response", {}).get("submitted_at"),
        },
    )

    result = await qualifier.qualify(lead)
    logger.info(f"Typeform lead qualified: {lead.email} -> {result.tier} ({result.score})")

    return WebhookResponse(
        status="qualified",
        lead_id=result.lead_id,
        score=result.score,
        message=f"Lead qualified as {result.tier}",
    )

@router.post("/calendly", response_model=WebhookResponse)
async def calendly_webhook(request: Request):
    """Handle meeting bookings from Calendly as lead signals."""
    data = await request.json()
    event = data.get("event", "")

    if event != "invitee.created":
        return WebhookResponse(status="skipped", message="Event type not relevant")

    payload = data.get("payload", {})
    invitee = payload.get("invitee", {})
    questions = {
        q["question"]: q["answer"]
        for q in payload.get("questions_and_answers", [])
    }

    lead = LeadInput(
        email=invitee.get("email", ""),
        company=questions.get("Company", questions.get("company", "")),
        name=invitee.get("name", ""),
        message=questions.get("What would you like to discuss?", "Meeting booked via Calendly"),
        source=LeadSource.CALENDLY,
        metadata={
            "event_type": payload.get("event_type", {}).get("name"),
            "event_url": payload.get("event", {}).get("uri"),
            "scheduled_at": payload.get("event", {}).get("start_time"),
            "timezone": invitee.get("timezone"),
        },
    )

    result = await qualifier.qualify(lead)
    logger.info(f"Calendly lead qualified: {lead.email} -> {result.tier} ({result.score})")

    return WebhookResponse(
        status="qualified",
        lead_id=result.lead_id,
        score=result.score,
        message=f"Lead qualified as {result.tier}",
    )


@router.post("/generic", response_model=WebhookResponse)
async def generic_webhook(
    request: Request,
    x_webhook_secret: Optional[str] = Header(None),
):
    """
    Generic webhook handler for custom integrations.
    Expects a JSON body with email, company, and optional fields.
    """
    if settings.generic_webhook_secret:
        if x_webhook_secret != settings.generic_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    data = await request.json()

    # Support flexible field mapping
    email = data.get("email") or data.get("contact_email") or data.get("lead_email", "")
    company = data.get("company") or data.get("company_name") or data.get("organization", "")
    name = data.get("name") or data.get("full_name") or data.get("contact_name", "")
    message = data.get("message") or data.get("notes") or data.get("description", "")

    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    lead = LeadInput(
        email=email,
        company=company,
        name=name,
        message=message,
        source=LeadSource.API,
        metadata={k: v for k, v in data.items() if k not in ("email", "company", "name", "message")},
    )

    result = await qualifier.qualify(lead)
    logger.info(f"Generic webhook lead qualified: {lead.email} -> {result.tier} ({result.score})")

    return WebhookResponse(
        status="qualified",
        lead_id=result.lead_id,
        score=result.score,
        message=f"Lead qualified as {result.tier}",
    )


@router.get("/health")
async def webhook_health():
    """Health check for webhook endpoints."""
    return {
        "status": "healthy",
        "endpoints": [
            "/api/webhooks/hubspot",
            "/api/webhooks/typeform",
            "/api/webhooks/calendly",
            "/api/webhooks/generic",
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
