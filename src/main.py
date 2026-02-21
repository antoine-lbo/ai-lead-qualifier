from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .qualifier import LeadQualifier
from .enrichment import enrich_lead
from .router import route_lead

app = FastAPI(title="AI Lead Qualifier", version="1.0.0")
qualifier = LeadQualifier()


class LeadInput(BaseModel):
    email: str
    company: str | None = None
    message: str | None = None
    source: str = "api"


class QualificationResult(BaseModel):
    score: int
    tier: str
    reasoning: str
    recommended_action: str
    enrichment: dict


@app.post("/api/qualify", response_model=QualificationResult)
async def qualify_lead(lead: LeadInput):
    """Qualify an inbound lead using AI scoring and enrichment data."""
    try:
        enrichment = await enrich_lead(lead.email, lead.company)
        result = await qualifier.score(lead, enrichment)
        await route_lead(result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/qualify/batch")
async def qualify_batch(leads: list[LeadInput]):
    """Process multiple leads in batch mode."""
    results = []
    for lead in leads:
        try:
            enrichment = await enrich_lead(lead.email, lead.company)
            result = await qualifier.score(lead, enrichment)
            await route_lead(result)
            results.append({"lead": lead.email, **result})
        except Exception as e:
            results.append({"lead": lead.email, "error": str(e)})
    return {"results": results, "total": len(results)}


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/stats")
async def stats():
    """Return qualification pipeline statistics."""
    return qualifier.get_stats()
