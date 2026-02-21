"""
Health check endpoints for monitoring and observability.

Provides:
  GET /health       - Quick liveness check
  GET /health/ready - Readiness check with dependency status
  GET /health/live  - Kubernetes liveness probe
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import redis.asyncio as redis
from fastapi import APIRouter, status
from pydantic import BaseModel


logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# Track app start time for uptime calculation
APP_START_TIME = time.time()
APP_VERSION = "1.0.0"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class DependencyCheck(BaseModel):
    name: str
    status: HealthStatus
    latency_ms: float
    message: Optional[str] = None


class HealthResponse(BaseModel):
    status: HealthStatus
    version: str
    uptime_seconds: float
    timestamp: str
    dependencies: Optional[list[DependencyCheck]] = None


# ---------- Dependency Checks ----------


async def check_database() -> DependencyCheck:
    """Check PostgreSQL connectivity."""
    start = time.perf_counter()
    try:
        import asyncpg
        from src.config import get_settings

        settings = get_settings()
        conn = await asyncio.wait_for(
            asyncpg.connect(settings.database_url),
            timeout=5.0,
        )
        await conn.fetchval("SELECT 1")
        await conn.close()

        latency = (time.perf_counter() - start) * 1000
        return DependencyCheck(
            name="postgresql",
            status=HealthStatus.HEALTHY,
            latency_ms=round(latency, 2),
        )
    except asyncio.TimeoutError:
        latency = (time.perf_counter() - start) * 1000
        return DependencyCheck(
            name="postgresql",
            status=HealthStatus.UNHEALTHY,
            latency_ms=round(latency, 2),
            message="Connection timeout (> 5s)",
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        logger.warning("Database health check failed: %s", str(e))
        return DependencyCheck(
            name="postgresql",
            status=HealthStatus.UNHEALTHY,
            latency_ms=round(latency, 2),
            message=str(e),
        )

async def check_redis() -> DependencyCheck:
    """Check Redis connectivity."""
    start = time.perf_counter()
    try:
        from src.config import get_settings

        settings = get_settings()
        client = redis.from_url(settings.redis_url, decode_responses=True)
        await asyncio.wait_for(client.ping(), timeout=3.0)
        info = await client.info("memory")
        await client.close()

        latency = (time.perf_counter() - start) * 1000
        used_mb = round(info.get("used_memory", 0) / 1024 / 1024, 1)
        return DependencyCheck(
            name="redis",
            status=HealthStatus.HEALTHY,
            latency_ms=round(latency, 2),
            message=f"Memory: {used_mb}MB",
        )
    except asyncio.TimeoutError:
        latency = (time.perf_counter() - start) * 1000
        return DependencyCheck(
            name="redis",
            status=HealthStatus.UNHEALTHY,
            latency_ms=round(latency, 2),
            message="Connection timeout (> 3s)",
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        logger.warning("Redis health check failed: %s", str(e))
        return DependencyCheck(
            name="redis",
            status=HealthStatus.DEGRADED,
            latency_ms=round(latency, 2),
            message=str(e),
        )


async def check_openai() -> DependencyCheck:
    """Check OpenAI API connectivity with a minimal request."""
    start = time.perf_counter()
    try:
        import httpx
        from src.config import get_settings

        settings = get_settings()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )

        latency = (time.perf_counter() - start) * 1000
        if response.status_code == 200:
            return DependencyCheck(
                name="openai",
                status=HealthStatus.HEALTHY,
                latency_ms=round(latency, 2),
            )
        else:
            return DependencyCheck(
                name="openai",
                status=HealthStatus.DEGRADED,
                latency_ms=round(latency, 2),
                message=f"HTTP {response.status_code}",
            )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return DependencyCheck(
            name="openai",
            status=HealthStatus.DEGRADED,
            latency_ms=round(latency, 2),
            message=str(e),
        )

# ---------- Route Handlers ----------


def _get_uptime() -> float:
    return round(time.time() - APP_START_TIME, 1)


def _aggregate_status(checks: list[DependencyCheck]) -> HealthStatus:
    """Determine overall health from individual checks."""
    statuses = [c.status for c in checks]
    if HealthStatus.UNHEALTHY in statuses:
        return HealthStatus.UNHEALTHY
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Quick health check",
    description="Returns basic service status. Use /health/ready for dependency checks.",
)
async def health_check() -> HealthResponse:
    """Basic liveness check — always returns quickly."""
    return HealthResponse(
        status=HealthStatus.HEALTHY,
        version=APP_VERSION,
        uptime_seconds=_get_uptime(),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="Readiness check",
    description="Checks all dependencies (DB, Redis, OpenAI). Returns 503 if unhealthy.",
    responses={503: {"description": "Service not ready"}},
)
async def readiness_check() -> HealthResponse:
    """Full readiness check — tests all external dependencies."""
    checks = await asyncio.gather(
        check_database(),
        check_redis(),
        check_openai(),
        return_exceptions=True,
    )

    # Handle any exceptions from gather
    resolved_checks = []
    for i, check in enumerate(checks):
        if isinstance(check, Exception):
            names = ["postgresql", "redis", "openai"]
            resolved_checks.append(
                DependencyCheck(
                    name=names[i],
                    status=HealthStatus.UNHEALTHY,
                    latency_ms=0,
                    message=f"Check failed: {str(check)}",
                )
            )
        else:
            resolved_checks.append(check)

    overall = _aggregate_status(resolved_checks)

    response = HealthResponse(
        status=overall,
        version=APP_VERSION,
        uptime_seconds=_get_uptime(),
        timestamp=datetime.now(timezone.utc).isoformat(),
        dependencies=resolved_checks,
    )

    if overall == HealthStatus.UNHEALTHY:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response.model_dump(),
        )

    return response


@router.get(
    "/health/live",
    summary="Liveness probe",
    description="Kubernetes liveness probe. Returns 200 if process is alive.",
)
async def liveness_probe():
    """Minimal liveness probe for container orchestrators."""
    return {"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()}
