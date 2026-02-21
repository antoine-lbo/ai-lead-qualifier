"""
Batch Processing Module

Process CSV uploads of historical leads for retroactive qualification.
Supports concurrent processing with configurable batch sizes and
real-time progress tracking via SSE (Server-Sent Events).
"""

import asyncio
import csv
import io
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.qualifier import qualify_lead
from src.models import LeadInput, QualifiedLead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/batch", tags=["batch"])


# ─── Models ───────────────────────────────────────────────────────


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchJob(BaseModel):
    """Tracks the state of a batch processing job."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: JobStatus = JobStatus.PENDING
    total_leads: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    batch_size: int = 10
    concurrency: int = 5

    @property
    def progress(self) -> float:
        if self.total_leads == 0:
            return 0.0
        return round(self.processed / self.total_leads * 100, 1)

    @property
    def elapsed_seconds(self) -> float:
        end = self.completed_at or datetime.now(timezone.utc)
        return (end - self.created_at).total_seconds()

    @property
    def leads_per_second(self) -> float:
        elapsed = self.elapsed_seconds
        if elapsed == 0:
            return 0.0
        return round(self.processed / elapsed, 2)


class BatchSummary(BaseModel):
    """Summary response for a completed batch job."""
    job_id: str
    status: JobStatus
    total_leads: int
    succeeded: int
    failed: int
    progress: float
    elapsed_seconds: float
    leads_per_second: float
    tier_breakdown: dict[str, int] = Field(default_factory=dict)
    avg_score: float = 0.0

# ─── In-Memory Job Store ──────────────────────────────────────────


_jobs: dict[str, BatchJob] = {}


def get_job(job_id: str) -> BatchJob:
    """Retrieve a batch job by ID."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _jobs[job_id]


# ─── CSV Parsing ──────────────────────────────────────────────────


REQUIRED_COLUMNS = {"email"}
OPTIONAL_COLUMNS = {"company", "first_name", "last_name", "message", "phone", "source"}


def parse_csv(content: str) -> list[dict]:
    """
    Parse CSV content into a list of lead dictionaries.
    Validates that required columns are present.
    """
    reader = csv.DictReader(io.StringIO(content))

    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file is empty or has no headers")

    headers = {h.strip().lower() for h in reader.fieldnames}
    missing = REQUIRED_COLUMNS - headers
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required columns: {missing}. Found: {headers}",
        )

    leads = []
    for i, row in enumerate(reader):
        # Normalize keys to lowercase
        normalized = {k.strip().lower(): v.strip() for k, v in row.items() if v}
        if normalized.get("email"):
            leads.append(normalized)
        else:
            logger.warning(f"Skipping row {i + 2}: missing email")

    if not leads:
        raise HTTPException(status_code=400, detail="No valid leads found in CSV")

    return leads


# ─── Batch Processing Engine ──────────────────────────────────────


async def process_lead(lead_data: dict, job: BatchJob) -> Optional[dict]:
    """Process a single lead with error isolation."""
    try:
        lead_input = LeadInput(
            email=lead_data["email"],
            company=lead_data.get("company"),
            message=lead_data.get("message"),
            first_name=lead_data.get("first_name"),
            last_name=lead_data.get("last_name"),
        )
        result = await qualify_lead(lead_input)
        job.succeeded += 1
        return result.model_dump()
    except Exception as e:
        logger.error(f"Failed to qualify {lead_data.get('email')}: {e}")
        job.failed += 1
        job.errors.append({
            "email": lead_data.get("email", "unknown"),
            "error": str(e),
        })
        return None
    finally:
        job.processed += 1


async def run_batch(job: BatchJob, leads: list[dict]) -> None:
    """
    Process leads in configurable batches with concurrency control.
    Uses asyncio.Semaphore to limit concurrent API calls.
    """
    job.status = JobStatus.PROCESSING
    semaphore = asyncio.Semaphore(job.concurrency)

    async def _process_with_semaphore(lead_data: dict) -> Optional[dict]:
        async with semaphore:
            return await process_lead(lead_data, job)

    try:
        # Process in batches for memory efficiency
        for i in range(0, len(leads), job.batch_size):
            batch = leads[i : i + job.batch_size]
            tasks = [_process_with_semaphore(lead) for lead in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, dict):
                    job.results.append(result)
                elif isinstance(result, Exception):
                    logger.error(f"Unexpected batch error: {result}")

            logger.info(
                f"Job {job.id}: processed {job.processed}/{job.total_leads} "
                f"({job.progress}%)"
            )

        job.status = JobStatus.COMPLETED
    except Exception as e:
        logger.error(f"Batch job {job.id} failed: {e}")
        job.status = JobStatus.FAILED
    finally:
        job.completed_at = datetime.now(timezone.utc)

# ─── API Endpoints ────────────────────────────────────────────────


@router.post("/upload", response_model=dict)
async def upload_csv(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    batch_size: int = 10,
    concurrency: int = 5,
):
    """
    Upload a CSV file of leads for batch qualification.

    The file must contain an "email" column. Optional columns:
    company, first_name, last_name, message, phone, source.

    Returns a job ID for tracking progress.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported")

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    leads = parse_csv(text)

    job = BatchJob(
        total_leads=len(leads),
        batch_size=min(batch_size, 50),
        concurrency=min(concurrency, 20),
    )
    _jobs[job.id] = job

    background_tasks.add_task(run_batch, job, leads)

    logger.info(f"Created batch job {job.id} with {len(leads)} leads")
    return {
        "job_id": job.id,
        "total_leads": len(leads),
        "status": job.status,
        "message": f"Processing {len(leads)} leads in background",
    }


@router.get("/jobs/{job_id}", response_model=BatchSummary)
async def get_job_status(job_id: str):
    """Get the current status and progress of a batch job."""
    job = get_job(job_id)

    tier_breakdown: dict[str, int] = {}
    total_score = 0
    for result in job.results:
        tier = result.get("tier", "UNKNOWN")
        tier_breakdown[tier] = tier_breakdown.get(tier, 0) + 1
        total_score += result.get("score", 0)

    return BatchSummary(
        job_id=job.id,
        status=job.status,
        total_leads=job.total_leads,
        succeeded=job.succeeded,
        failed=job.failed,
        progress=job.progress,
        elapsed_seconds=job.elapsed_seconds,
        leads_per_second=job.leads_per_second,
        tier_breakdown=tier_breakdown,
        avg_score=round(total_score / max(job.succeeded, 1), 1),
    )


@router.get("/jobs/{job_id}/results")
async def get_job_results(job_id: str, tier: Optional[str] = None):
    """
    Get the qualification results for a batch job.
    Optionally filter by tier (HOT, WARM, COLD).
    """
    job = get_job(job_id)
    results = job.results

    if tier:
        tier_upper = tier.upper()
        results = [r for r in results if r.get("tier") == tier_upper]

    return {
        "job_id": job.id,
        "total": len(results),
        "results": results,
    }


@router.get("/jobs/{job_id}/stream")
async def stream_progress(job_id: str):
    """Stream real-time progress updates via Server-Sent Events."""
    job = get_job(job_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        last_processed = -1
        while True:
            if job.processed != last_processed:
                last_processed = job.processed
                data = (
                    f'{{"processed": {job.processed}, "total": {job.total_leads}, '
                    f'"progress": {job.progress}, "status": "{job.status.value}", '
                    f'"succeeded": {job.succeeded}, "failed": {job.failed}}}'
                )
                yield f"data: {data}\n\n"

            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                yield f"data: {{"status": "{job.status.value}", "done": true}}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}/export")
async def export_results_csv(job_id: str):
    """Export batch results as a downloadable CSV file."""
    job = get_job(job_id)

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Job not yet completed")

    if not job.results:
        raise HTTPException(status_code=404, detail="No results to export")

    output = io.StringIO()
    fieldnames = list(job.results[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(job.results)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=batch_{job_id}_results.csv"
        },
    )


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a running batch job."""
    job = get_job(job_id)
    if job.status == JobStatus.PROCESSING:
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(timezone.utc)
        return {"message": f"Job {job_id} cancelled", "processed": job.processed}
    return {"message": f"Job {job_id} is not running (status: {job.status})"}
