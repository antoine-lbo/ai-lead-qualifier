"""
Rate Limiting Middleware for AI Lead Qualifier

Implements tiered rate limiting using Redis with sliding window
algorithm. Supports per-API-key and per-IP rate limits with
configurable tiers (Free, Pro, Enterprise).
"""

import time
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from enum import Enum

import redis.asyncio as redis
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import settings

logger = logging.getLogger(__name__)


class RateLimitTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True)
class RateLimitConfig:
    requests_per_second: int
    requests_per_minute: int
    requests_per_hour: int
    requests_per_day: int
    burst_size: int
    concurrent_requests: int


TIER_LIMITS: dict[RateLimitTier, RateLimitConfig] = {
    RateLimitTier.FREE: RateLimitConfig(
        requests_per_second=2, requests_per_minute=30,
        requests_per_hour=500, requests_per_day=1_000,
        burst_size=5, concurrent_requests=2,
    ),
    RateLimitTier.PRO: RateLimitConfig(
        requests_per_second=10, requests_per_minute=200,
        requests_per_hour=5_000, requests_per_day=50_000,
        burst_size=20, concurrent_requests=10,
    ),
    RateLimitTier.ENTERPRISE: RateLimitConfig(
        requests_per_second=50, requests_per_minute=1_000,
        requests_per_hour=30_000, requests_per_day=500_000,
        burst_size=100, concurrent_requests=50,
    ),
}


class SlidingWindowRateLimiter:
    """Redis-based sliding window rate limiter using sorted sets."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def is_allowed(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, dict]:
        now = time.time()
        window_start = now - window_seconds
        redis_key = f"ratelimit:{key}:{window_seconds}"

        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zcard(redis_key)
        pipe.zadd(redis_key, {str(now): now})
        pipe.expire(redis_key, window_seconds + 1)
        results = await pipe.execute()
        current_count = results[1]

        remaining = max(0, limit - current_count - 1)
        reset_at = int(now + window_seconds)
        info = {"limit": limit, "remaining": remaining, "reset": reset_at, "window": window_seconds}

        if current_count >= limit:
            await self.redis.zrem(redis_key, str(now))
            return False, info
        return True, info

    async def get_usage(self, key: str, window_seconds: int) -> int:
        now = time.time()
        redis_key = f"ratelimit:{key}:{window_seconds}"
        return await self.redis.zcount(redis_key, now - window_seconds, now)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware for tiered rate limiting."""

    EXEMPT_PATHS = {"/health", "/health/detailed", "/docs", "/openapi.json"}

    def __init__(self, app, redis_url: Optional[str] = None):
        super().__init__(app)
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: Optional[redis.Redis] = None
        self._limiter: Optional[SlidingWindowRateLimiter] = None

    async def _get_limiter(self) -> SlidingWindowRateLimiter:
        if self._limiter is None:
            self._redis = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
            self._limiter = SlidingWindowRateLimiter(self._redis)
        return self._limiter

    def _extract_identifier(self, request: Request) -> Tuple[str, RateLimitTier]:
        api_key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        if api_key and api_key.startswith("sk_"):
            if api_key.startswith("sk_enterprise_"):
                return f"key:{api_key[:24]}", RateLimitTier.ENTERPRISE
            elif api_key.startswith("sk_pro_"):
                return f"key:{api_key[:20]}", RateLimitTier.PRO
            return f"key:{api_key[:20]}", RateLimitTier.FREE

        client_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        return f"ip:{client_ip}", RateLimitTier.FREE

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        try:
            limiter = await self._get_limiter()
        except Exception as e:
            logger.error(f"Rate limiter Redis connection failed: {e}")
            return await call_next(request)

        identifier, tier = self._extract_identifier(request)
        config = TIER_LIMITS[tier]

        windows = [
            (config.requests_per_second, 1, "per_second"),
            (config.requests_per_minute, 60, "per_minute"),
            (config.requests_per_hour, 3600, "per_hour"),
            (config.requests_per_day, 86400, "per_day"),
        ]

        rate_limit_info = {}
        for limit, window, name in windows:
            allowed, info = await limiter.is_allowed(f"{identifier}:{name}", limit, window)
            if not allowed:
                retry_after = info["reset"] - int(time.time())
                logger.warning(f"Rate limit exceeded: {identifier} ({name}: {info['limit']} per {window}s)")
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"error": {"code": "RATE_LIMITED", "message": f"Rate limit exceeded. Try again in {retry_after}s.", "tier": tier.value, "limit": info["limit"], "window": name, "retry_after": retry_after}},
                    headers={"X-RateLimit-Limit": str(info["limit"]), "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(info["reset"]), "Retry-After": str(retry_after)},
                )
            rate_limit_info[name] = info

        response = await call_next(request)

        minute_info = rate_limit_info.get("per_minute", {})
        if minute_info:
            response.headers["X-RateLimit-Limit"] = str(minute_info.get("limit", ""))
            response.headers["X-RateLimit-Remaining"] = str(minute_info.get("remaining", ""))
            response.headers["X-RateLimit-Reset"] = str(minute_info.get("reset", ""))
        response.headers["X-RateLimit-Tier"] = tier.value
        return response
