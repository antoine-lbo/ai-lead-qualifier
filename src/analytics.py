"""
Analytics module for tracking lead qualification metrics.
Provides real-time dashboards and historical reporting.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from .config import settings

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class PipelineMetrics(BaseModel):
    total_leads: int
    qualified_leads: int
    hot_leads: int
    warm_leads: int
    cold_leads: int
    qualification_rate: float
    avg_score: float
    avg_processing_time_ms: float
    leads_per_hour: float


class TierBreakdown(BaseModel):
    tier: str
    count: int
    percentage: float
    avg_score: float
    top_industries: list[str]


class ConversionFunnel(BaseModel):
    stage: str
    count: int
    conversion_rate: float
    avg_time_in_stage_hours: float


class DailyTrend(BaseModel):
    date: str
    total: int
    hot: int
    warm: int
    cold: int
    avg_score: float


async def get_db_pool():
    """Get or create database connection pool."""
    if not hasattr(get_db_pool, "_pool"):
        get_db_pool._pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=2,
            max_size=10,
        )
    return get_db_pool._pool


@router.get("/overview", response_model=PipelineMetrics)
async def get_overview(
    days: int = Query(default=30, ge=1, le=365),
):
    """Get pipeline overview metrics for the specified time period."""
    pool = await get_db_pool()
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as total_leads,
                COUNT(*) FILTER (WHERE score >= 50) as qualified_leads,
                COUNT(*) FILTER (WHERE tier = 'HOT') as hot_leads,
                COUNT(*) FILTER (WHERE tier = 'WARM') as warm_leads,
                COUNT(*) FILTER (WHERE tier = 'COLD') as cold_leads,
                ROUND(AVG(score)::numeric, 1) as avg_score,
                ROUND(AVG(processing_time_ms)::numeric, 0) as avg_processing_time_ms
            FROM lead_qualifications
            WHERE created_at >= $1
            """,
            cutoff,
        )

        total = row["total_leads"] or 0
        qualified = row["qualified_leads"] or 0
        hours_elapsed = max((datetime.utcnow() - cutoff).total_seconds() / 3600, 1)

        return PipelineMetrics(
            total_leads=total,
            qualified_leads=qualified,
            hot_leads=row["hot_leads"] or 0,
            warm_leads=row["warm_leads"] or 0,
            cold_leads=row["cold_leads"] or 0,
            qualification_rate=round(qualified / total * 100, 1) if total > 0 else 0,
            avg_score=float(row["avg_score"] or 0),
            avg_processing_time_ms=float(row["avg_processing_time_ms"] or 0),
            leads_per_hour=round(total / hours_elapsed, 1),
        )

@router.get("/trends", response_model=list[DailyTrend])
async def get_daily_trends(
    days: int = Query(default=14, ge=1, le=90),
):
    """Get daily lead qualification trends."""
    pool = await get_db_pool()
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                DATE(created_at) as date,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE tier = 'HOT') as hot,
                COUNT(*) FILTER (WHERE tier = 'WARM') as warm,
                COUNT(*) FILTER (WHERE tier = 'COLD') as cold,
                ROUND(AVG(score)::numeric, 1) as avg_score
            FROM lead_qualifications
            WHERE created_at >= $1
            GROUP BY DATE(created_at)
            ORDER BY date DESC
            """,
            cutoff,
        )

        return [
            DailyTrend(
                date=row["date"].isoformat(),
                total=row["total"],
                hot=row["hot"],
                warm=row["warm"],
                cold=row["cold"],
                avg_score=float(row["avg_score"] or 0),
            )
            for row in rows
        ]


@router.get("/tiers", response_model=list[TierBreakdown])
async def get_tier_breakdown(
    days: int = Query(default=30, ge=1, le=365),
):
    """Get detailed breakdown by qualification tier."""
    pool = await get_db_pool()
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with pool.acquire() as conn:
        total_count = await conn.fetchval(
            "SELECT COUNT(*) FROM lead_qualifications WHERE created_at >= $1",
            cutoff,
        )

        rows = await conn.fetch(
            """
            SELECT
                tier,
                COUNT(*) as count,
                ROUND(AVG(score)::numeric, 1) as avg_score,
                ARRAY(
                    SELECT industry
                    FROM (
                        SELECT enrichment_data->>'industry' as industry,
                               COUNT(*) as cnt
                        FROM lead_qualifications lq2
                        WHERE lq2.tier = lq.tier AND lq2.created_at >= $1
                        GROUP BY enrichment_data->>'industry'
                        ORDER BY cnt DESC
                        LIMIT 3
                    ) sub
                ) as top_industries
            FROM lead_qualifications lq
            WHERE created_at >= $1
            GROUP BY tier
            ORDER BY count DESC
            """,
            cutoff,
        )

        return [
            TierBreakdown(
                tier=row["tier"],
                count=row["count"],
                percentage=round(row["count"] / total_count * 100, 1) if total_count > 0 else 0,
                avg_score=float(row["avg_score"] or 0),
                top_industries=row["top_industries"] or [],
            )
            for row in rows
        ]


@router.get("/funnel", response_model=list[ConversionFunnel])
async def get_conversion_funnel(
    days: int = Query(default=30, ge=1, le=365),
):
    """Get conversion funnel metrics from lead to customer."""
    pool = await get_db_pool()
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with pool.acquire() as conn:
        stages = await conn.fetch(
            """
            WITH funnel AS (
                SELECT
                    'qualified' as stage, COUNT(*) as count,
                    AVG(EXTRACT(EPOCH FROM (routed_at - created_at)) / 3600) as avg_hours
                FROM lead_qualifications WHERE created_at >= $1 AND score >= 50
                UNION ALL
                SELECT
                    'contacted' as stage, COUNT(*) as count,
                    AVG(EXTRACT(EPOCH FROM (contacted_at - routed_at)) / 3600) as avg_hours
                FROM lead_qualifications WHERE created_at >= $1 AND contacted_at IS NOT NULL
                UNION ALL
                SELECT
                    'meeting_booked' as stage, COUNT(*) as count,
                    AVG(EXTRACT(EPOCH FROM (meeting_at - contacted_at)) / 3600) as avg_hours
                FROM lead_qualifications WHERE created_at >= $1 AND meeting_at IS NOT NULL
                UNION ALL
                SELECT
                    'converted' as stage, COUNT(*) as count,
                    AVG(EXTRACT(EPOCH FROM (converted_at - meeting_at)) / 3600) as avg_hours
                FROM lead_qualifications WHERE created_at >= $1 AND converted_at IS NOT NULL
            )
            SELECT * FROM funnel ORDER BY count DESC
            """,
            cutoff,
        )

        total = stages[0]["count"] if stages else 1
        return [
            ConversionFunnel(
                stage=row["stage"],
                count=row["count"],
                conversion_rate=round(row["count"] / total * 100, 1),
                avg_time_in_stage_hours=round(float(row["avg_hours"] or 0), 1),
            )
            for row in stages
        ]


@router.get("/top-sources")
async def get_top_sources(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=10, ge=1, le=50),
):
    """Get top lead sources by volume and quality."""
    pool = await get_db_pool()
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                source,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE tier = 'HOT') as hot_count,
                ROUND(AVG(score)::numeric, 1) as avg_score,
                ROUND(
                    COUNT(*) FILTER (WHERE converted_at IS NOT NULL)::numeric /
                    NULLIF(COUNT(*), 0) * 100, 1
                ) as conversion_rate
            FROM lead_qualifications
            WHERE created_at >= $1
            GROUP BY source
            ORDER BY total DESC
            LIMIT $2
            """,
            cutoff,
            limit,
        )

        return [
            {
                "source": row["source"],
                "total": row["total"],
                "hot_count": row["hot_count"],
                "avg_score": float(row["avg_score"] or 0),
                "conversion_rate": float(row["conversion_rate"] or 0),
            }
            for row in rows
        ]
