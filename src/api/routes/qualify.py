"""
Lead Qualification API Routes

FastAPI router for lead qualification endpoints including single lead
qualification, batch processing, and qualification status retrieval.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field, validator

from src.core.scoring import ScoringEngine, QualificationResult
from src.api.services.enrichment import EnrichmentService, EnrichmentResult
from src.api.middleware.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["qualification"])


# =============================================================================
# Request / Response Models
# =============================================================================


class LeadInput(BaseModel):
    """Inbound lead data for qualification."""
    email: EmailStr
    name: Optional[str] = None
    company: Optional[str] = None
    message: Optional[str] = None
    phone: Optional[str] = None
    job_title: Optional[str] = None
    company_size: Optional[str] = None
    industry: Optional[str] = None
    source: str = Field(default="api", description="Lead source channel")
    metadata: dict = Field(default_factory=dict)

    @validator("email")
    def normalize_email(cls, v: str) -> str:
        return v.lower().strip()

    @validator("company")
    def normalize_company(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else None


class QualificationResponse(BaseModel):
    """Response for a single lead qualification."""
    qualification_id: str
    score: int
    tier: str
    reasoning: str
    recommended_action: str
    enrichment: dict
    scoring_breakdown: dict
    processing_time_ms: int


class BatchQualificationRequest(BaseModel):
    """Batch qualification request with multiple leads."""
    leads: list[LeadInput] = Field(..., max_length=100)
    webhook_url: Optional[str] = None
    priority: str = Field(default="normal", pattern="^(low|normal|high)$")


class BatchStatus(BaseModel):
    """Status of a batch qualification job."""
    batch_id: str
    status: str
    total_leads: int
    processed: int
    results: Optional[list[QualificationResponse]] = None
# =============================================================================
# Dependencies
# =============================================================================

_scoring_engine: Optional[ScoringEngine] = None
_enrichment_service: Optional[EnrichmentService] = None
_batch_jobs: dict[str, BatchStatus] = {}


def get_scoring_engine() -> ScoringEngine:
    global _scoring_engine
    if _scoring_engine is None:
        _scoring_engine = ScoringEngine()
    return _scoring_engine


def get_enrichment_service() -> EnrichmentService:
    global _enrichment_service
    if _enrichment_service is None:
        _enrichment_service = EnrichmentService()
    return _enrichment_service


# =============================================================================
# Routes
# =============================================================================


@router.post("/qualify", response_model=QualificationResponse)
async def qualify_lead(
    lead: LeadInput,
    scoring: ScoringEngine = Depends(get_scoring_engine),
    enrichment: EnrichmentService = Depends(get_enrichment_service),
) -> QualificationResponse:
    """
    Qualify a single inbound lead.

    Enriches the lead with company data, runs AI-powered scoring,
    and returns qualification tier with routing recommendation.
    """
    start_time = time.monotonic()
    qualification_id = f"qual_{uuid4().hex[:12]}"

    try:
        # Step 1: Enrich lead data
        enrichment_data = await enrichment.enrich(
            email=lead.email,
            company=lead.company,
            name=lead.name,
        )

        # Step 2: Run qualification scoring
        result: QualificationResult = await scoring.qualify(
            email=lead.email,
            company=lead.company,
            message=lead.message,
            job_title=lead.job_title or enrichment_data.person.title if enrichment_data.person else lead.job_title,
            enrichment=enrichment_data,
        )

        processing_time = int((time.monotonic() - start_time) * 1000)

        logger.info(
            "Lead qualified",
            extra={
                "qualification_id": qualification_id,
                "email": lead.email,
                "score": result.score,
                "tier": result.tier.value,
                "processing_time_ms": processing_time,
            },
        )

        return QualificationResponse(
            qualification_id=qualification_id,
            score=result.score,
            tier=result.tier.value,
            reasoning=result.reasoning,
            recommended_action=result.action.value,
            enrichment=enrichment_data.dict() if enrichment_data else {},
            scoring_breakdown=result.breakdown.dict(),
            processing_time_ms=processing_time,
        )

    except Exception as e:
        logger.error(f"Qualification failed for {lead.email}: {e}")
        raise HTTPException(status_code=500, detail=f"Qualification failed: {str(e)}")

@router.post("/qualify/batch", response_model=BatchStatus)
async def qualify_batch(
    request: BatchQualificationRequest,
    background_tasks: BackgroundTasks,
) -> BatchStatus:
    """
    Submit a batch of leads for async qualification.

    Returns a batch_id for tracking progress. Results are available
    via GET /api/qualify/batch/{batch_id} or sent to webhook_url.
    """
    batch_id = f"batch_{uuid4().hex[:12]}"

    status = BatchStatus(
        batch_id=batch_id,
        status="processing",
        total_leads=len(request.leads),
        processed=0,
    )
    _batch_jobs[batch_id] = status

    background_tasks.add_task(
        _process_batch,
        batch_id=batch_id,
        leads=request.leads,
        webhook_url=request.webhook_url,
    )

    logger.info(f"Batch job {batch_id} started with {len(request.leads)} leads")
    return status


@router.get("/qualify/batch/{batch_id}", response_model=BatchStatus)
async def get_batch_status(batch_id: str) -> BatchStatus:
    """Get the status and results of a batch qualification job."""
    if batch_id not in _batch_jobs:
        raise HTTPException(status_code=404, detail=f"Batch job {batch_id} not found")
    return _batch_jobs[batch_id]


@router.post("/qualify/csv")
async def qualify_csv(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
) -> BatchStatus:
    """
    Upload a CSV file of leads for batch qualification.

    CSV must have at minimum an "email" column. Optional columns:
    name, company, message, job_title, company_size, industry, source.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    content = await file.read()
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        decoded = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(decoded))

    if "email" not in (reader.fieldnames or []):
        raise HTTPException(status_code=400, detail="CSV must have an email column")

    leads = []
    for row in reader:
        leads.append(LeadInput(
            email=row["email"],
            name=row.get("name"),
            company=row.get("company"),
            message=row.get("message"),
            job_title=row.get("job_title"),
            company_size=row.get("company_size"),
            industry=row.get("industry"),
            source=row.get("source", "csv_upload"),
        ))

    if len(leads) > 1000:
        raise HTTPException(status_code=400, detail="CSV exceeds maximum of 1000 leads")

    if not leads:
        raise HTTPException(status_code=400, detail="CSV contains no valid leads")

    batch_request = BatchQualificationRequest(leads=leads[:100])
    return await qualify_batch(batch_request, background_tasks)


@router.get("/qualify/export/{batch_id}")
async def export_batch_results(batch_id: str) -> StreamingResponse:
    """Export batch qualification results as a CSV file."""
    if batch_id not in _batch_jobs:
        raise HTTPException(status_code=404, detail="Batch not found")

    job = _batch_jobs[batch_id]
    if job.status != "completed" or not job.results:
        raise HTTPException(status_code=400, detail="Batch not yet completed")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "qualification_id", "score", "tier", "reasoning",
        "recommended_action", "processing_time_ms",
    ])
    writer.writeheader()
    for result in job.results:
        writer.writerow(result.dict(exclude={"enrichment", "scoring_breakdown"}))

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={batch_id}_results.csv"},
    )


@router.get("/health")
async def health_check() -> dict:
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "service": "ai-lead-qualifier",
        "version": "1.0.0",
        "active_batch_jobs": len([j for j in _batch_jobs.values() if j.status == "processing"]),
    }

# =============================================================================
# Background Tasks
# =============================================================================


async def _process_batch(
    batch_id: str,
    leads: list[LeadInput],
    webhook_url: Optional[str] = None,
) -> None:
    """Process a batch of leads asynchronously with concurrency control."""
    scoring = get_scoring_engine()
    enrichment = get_enrichment_service()
    results: list[QualificationResponse] = []
    semaphore = asyncio.Semaphore(10)  # Max 10 concurrent qualifications

    async def process_single(lead: LeadInput) -> Optional[QualificationResponse]:
        async with semaphore:
            try:
                start = time.monotonic()
                qid = f"qual_{uuid4().hex[:12]}"

                enrichment_data = await enrichment.enrich(
                    email=lead.email,
                    company=lead.company,
                    name=lead.name,
                )

                result = await scoring.qualify(
                    email=lead.email,
                    company=lead.company,
                    message=lead.message,
                    job_title=lead.job_title,
                    enrichment=enrichment_data,
                )

                elapsed = int((time.monotonic() - start) * 1000)

                return QualificationResponse(
                    qualification_id=qid,
                    score=result.score,
                    tier=result.tier.value,
                    reasoning=result.reasoning,
                    recommended_action=result.action.value,
                    enrichment=enrichment_data.dict() if enrichment_data else {},
                    scoring_breakdown=result.breakdown.dict(),
                    processing_time_ms=elapsed,
                )
            except Exception as e:
                logger.warning(f"Failed to qualify {lead.email}: {e}")
                return None

    tasks = [process_single(lead) for lead in leads]
    completed = await asyncio.gather(*tasks)

    results = [r for r in completed if r is not None]

    job = _batch_jobs[batch_id]
    job.status = "completed"
    job.processed = len(results)
    job.results = results

    logger.info(
        f"Batch {batch_id} completed: {len(results)}/{len(leads)} leads qualified"
    )

    # Send webhook notification if configured
    if webhook_url:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    webhook_url,
                    json={
                        "batch_id": batch_id,
                        "status": "completed",
                        "total": len(leads),
                        "processed": len(results),
                        "summary": {
                            "hot": len([r for r in results if r.tier == "HOT"]),
                            "warm": len([r for r in results if r.tier == "WARM"]),
                            "cold": len([r for r in results if r.tier == "COLD"]),
                        },
                    },
                    timeout=30,
                )
        except Exception as e:
            logger.error(f"Webhook delivery failed for {batch_id}: {e}")
