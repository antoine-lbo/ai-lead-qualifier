"""
Redis-backed rate limiter with sliding window algorithm.
Protects the API from abuse and manages OpenAI token budgets.
"""

import time
import logging
from typing import Optional

import redis.asyncio as redis
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding window rate limiter using Redis sorted sets."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        default_limit: int = 100,
        default_window: int = 3600,
    ):
        self.redis_url = redis_url or settings.redis_url
        self.default_limit = default_limit
        self.default_window = default_window
        self._redis: Optional[redis.Redis] = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._redis

    async def check_rate_limit(
        self,
        key: str,
        limit: Optional[int] = None,
        window: Optional[int] = None,
    ) -> dict:
        """
        Check if a request is within rate limits using sliding window.

        Returns:
            dict with keys: allowed, remaining, reset_at, limit
        """
        r = await self._get_redis()
        limit = limit or self.default_limit
        window = window or self.default_window
        now = time.time()
        window_start = now - window

        pipe = r.pipeline()

        # Remove expired entries
        pipe.zremrangebyscore(key, 0, window_start)
        # Count current entries
        pipe.zcard(key)
        # Add current request
        pipe.zadd(key, {str(now): now})
        # Set TTL on the key
        pipe.expire(key, window)

        results = await pipe.execute()
        current_count = results[1]

        allowed = current_count < limit
        remaining = max(0, limit - current_count - 1)

        if not allowed:
            # Remove the request we just added since it was denied
            await r.zrem(key, str(now))
            remaining = 0

        return {
            "allowed": allowed,
            "remaining": remaining,
            "reset_at": int(now + window),
            "limit": limit,
            "current": current_count,
        }

    async def get_usage(self, key: str, window: Optional[int] = None) -> int:
        """Get current usage count for a key."""
        r = await self._get_redis()
        window = window or self.default_window
        window_start = time.time() - window
        await r.zremrangebyscore(key, 0, window_start)
        return await r.zcard(key)

    async def reset(self, key: str) -> None:
        """Reset rate limit for a key."""
        r = await self._get_redis()
        await r.delete(key)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()


# Rate limit tiers per API key
RATE_LIMIT_TIERS = {
    "free": {"requests_per_hour": 50, "requests_per_day": 200},
    "starter": {"requests_per_hour": 500, "requests_per_day": 5000},
    "pro": {"requests_per_hour": 2000, "requests_per_day": 25000},
    "enterprise": {"requests_per_hour": 10000, "requests_per_day": 100000},
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware for automatic rate limiting."""

    def __init__(self, app, limiter: Optional[RateLimiter] = None):
        super().__init__(app)
        self.limiter = limiter or RateLimiter()

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks
        if request.url.path in ("/health", "/metrics", "/docs", "/openapi.json"):
            return await call_next(request)

        # Extract API key from header or query param
        api_key = (
            request.headers.get("X-API-Key")
            or request.query_params.get("api_key")
            or "anonymous"
        )

        # Determine rate limit tier
        tier = await self._get_tier(api_key)
        tier_limits = RATE_LIMIT_TIERS.get(tier, RATE_LIMIT_TIERS["free"])

        # Check hourly rate limit
        hourly_key = f"ratelimit:{api_key}:hourly"
        hourly_result = await self.limiter.check_rate_limit(
            hourly_key,
            limit=tier_limits["requests_per_hour"],
            window=3600,
        )

        if not hourly_result["allowed"]:
            logger.warning(f"Rate limit exceeded for {api_key} (hourly)")
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "message": "Hourly rate limit exceeded",
                    "limit": hourly_result["limit"],
                    "reset_at": hourly_result["reset_at"],
                    "tier": tier,
                },
            )

        # Add rate limit headers to response
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(hourly_result["limit"])
        response.headers["X-RateLimit-Remaining"] = str(hourly_result["remaining"])
        response.headers["X-RateLimit-Reset"] = str(hourly_result["reset_at"])
        response.headers["X-RateLimit-Tier"] = tier

        return response

    async def _get_tier(self, api_key: str) -> str:
        """Look up API key tier from Redis cache or database."""
        if api_key == "anonymous":
            return "free"

        r = await self.limiter._get_redis()
        cached_tier = await r.get(f"apikey:tier:{api_key}")
        if cached_tier:
            return cached_tier

        # Default to free tier if not found
        return "free"
