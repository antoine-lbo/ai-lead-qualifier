"""
Tests for the rate limiting module.

Covers sliding window rate limiting, per-client limits,
IP-based throttling, and Redis-backed distributed limiting.
"""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from src.rate_limiter import (
    RateLimiter,
    RateLimitConfig,
    SlidingWindowCounter,
    TokenBucket,
    RateLimitExceeded,
    RateLimitMiddleware,
    get_client_identifier,
)


# ─── Fixtures ────────────────────────────────────────────


@pytest.fixture
def rate_config():
    """Default rate limit configuration."""
    return RateLimitConfig(
        requests_per_minute=60,
        requests_per_hour=1000,
        burst_size=10,
        slowdown_threshold=0.8,
    )


@pytest.fixture
def mock_redis():
    """Mock Redis client for distributed rate limiting."""
    redis = AsyncMock()
    redis.pipeline.return_value.__aenter__ = AsyncMock(
        return_value=AsyncMock()
    )
    redis.pipeline.return_value.__aexit__ = AsyncMock(
        return_value=None
    )
    return redis


@pytest.fixture
def sliding_window(mock_redis):
    """Sliding window counter backed by mock Redis."""
    return SlidingWindowCounter(
        redis=mock_redis,
        window_size=60,
        max_requests=60,
    )


@pytest.fixture
def token_bucket():
    """Token bucket rate limiter."""
    return TokenBucket(
        capacity=10,
        refill_rate=1.0,
    )


@pytest.fixture
def rate_limiter(mock_redis, rate_config):
    """Full rate limiter instance."""
    return RateLimiter(
        redis=mock_redis,
        config=rate_config,
    )

# ─── Sliding Window Tests ─────────────────────────────────


class TestSlidingWindowCounter:
    """Tests for sliding window rate limiting algorithm."""

    @pytest.mark.asyncio
    async def test_allows_request_under_limit(self, sliding_window, mock_redis):
        """Should allow requests when under the limit."""
        pipe = AsyncMock()
        pipe.execute = AsyncMock(return_value=[1, True, 30])
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        allowed, remaining = await sliding_window.check("client:123")

        assert allowed is True
        assert remaining == 59

    @pytest.mark.asyncio
    async def test_blocks_request_over_limit(self, sliding_window, mock_redis):
        """Should block requests when limit is exceeded."""
        pipe = AsyncMock()
        pipe.execute = AsyncMock(return_value=[61, True, 30])
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        allowed, remaining = await sliding_window.check("client:123")

        assert allowed is False
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_window_expiration(self, sliding_window, mock_redis):
        """Should reset count after window expires."""
        pipe = AsyncMock()
        # First call: at limit
        pipe.execute = AsyncMock(return_value=[60, True, 1])
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        allowed, _ = await sliding_window.check("client:123")
        assert allowed is True

        # After window reset
        pipe.execute = AsyncMock(return_value=[1, True, 60])
        allowed, remaining = await sliding_window.check("client:123")
        assert allowed is True
        assert remaining == 59

    @pytest.mark.asyncio
    async def test_redis_connection_failure(self, sliding_window, mock_redis):
        """Should fail open when Redis is unavailable."""
        mock_redis.pipeline.side_effect = ConnectionError("Redis down")

        allowed, remaining = await sliding_window.check("client:123")

        # Fail open: allow the request
        assert allowed is True
        assert remaining == -1  # Unknown remaining

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, sliding_window, mock_redis):
        """Should handle concurrent rate limit checks."""
        call_count = 0

        async def mock_execute():
            nonlocal call_count
            call_count += 1
            return [call_count, True, 60]

        pipe = AsyncMock()
        pipe.execute = mock_execute
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        results = await asyncio.gather(
            *[sliding_window.check("client:123") for _ in range(5)]
        )

        assert len(results) == 5
        assert all(allowed for allowed, _ in results)

# ─── Token Bucket Tests ───────────────────────────────────


class TestTokenBucket:
    """Tests for token bucket algorithm."""

    def test_initial_capacity(self, token_bucket):
        """Should start with full capacity."""
        assert token_bucket.tokens == 10
        assert token_bucket.capacity == 10

    def test_consume_token(self, token_bucket):
        """Should consume tokens on each request."""
        assert token_bucket.consume() is True
        assert token_bucket.tokens == 9

    def test_consume_multiple_tokens(self, token_bucket):
        """Should consume multiple tokens at once."""
        assert token_bucket.consume(5) is True
        assert token_bucket.tokens == 5

    def test_reject_when_empty(self, token_bucket):
        """Should reject when no tokens available."""
        for _ in range(10):
            token_bucket.consume()

        assert token_bucket.consume() is False
        assert token_bucket.tokens == 0

    def test_reject_insufficient_tokens(self, token_bucket):
        """Should reject when requesting more tokens than available."""
        token_bucket.consume(8)
        assert token_bucket.consume(5) is False
        assert token_bucket.tokens == 2

    @patch("src.rate_limiter.time.monotonic")
    def test_token_refill(self, mock_time, token_bucket):
        """Should refill tokens over time."""
        mock_time.return_value = 0.0
        token_bucket.consume(10)
        assert token_bucket.tokens == 0

        # Advance 5 seconds → 5 tokens refilled at rate 1.0/sec
        mock_time.return_value = 5.0
        token_bucket._refill()
        assert token_bucket.tokens == 5

    @patch("src.rate_limiter.time.monotonic")
    def test_refill_caps_at_capacity(self, mock_time, token_bucket):
        """Should never exceed capacity after refill."""
        mock_time.return_value = 0.0
        token_bucket.consume(3)

        # Advance 20 seconds → would be 17+7=24 but capped at 10
        mock_time.return_value = 20.0
        token_bucket._refill()
        assert token_bucket.tokens == 10

    def test_burst_handling(self, token_bucket):
        """Should handle burst traffic up to capacity."""
        results = [token_bucket.consume() for _ in range(15)]

        assert results[:10] == [True] * 10
        assert results[10:] == [False] * 5

# ─── Rate Limiter Integration Tests ──────────────────────


class TestRateLimiter:
    """Tests for the main RateLimiter orchestrator."""

    @pytest.mark.asyncio
    async def test_check_rate_limit_allowed(self, rate_limiter, mock_redis):
        """Should allow requests within limits."""
        pipe = AsyncMock()
        pipe.execute = AsyncMock(return_value=[5, True, 55])
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        result = await rate_limiter.check("api_key:abc123")

        assert result.allowed is True
        assert result.remaining > 0
        assert result.retry_after is None

    @pytest.mark.asyncio
    async def test_check_rate_limit_exceeded(self, rate_limiter, mock_redis):
        """Should block requests over the limit with retry-after."""
        pipe = AsyncMock()
        pipe.execute = AsyncMock(return_value=[61, True, 30])
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        result = await rate_limiter.check("api_key:abc123")

        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after > 0

    @pytest.mark.asyncio
    async def test_per_endpoint_limits(self, rate_limiter, mock_redis):
        """Should enforce separate limits per endpoint."""
        pipe = AsyncMock()
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        # /api/qualify has stricter limits
        pipe.execute = AsyncMock(return_value=[25, True, 35])
        result_qualify = await rate_limiter.check(
            "api_key:abc123", endpoint="/api/qualify"
        )

        # /api/batch has looser limits
        pipe.execute = AsyncMock(return_value=[5, True, 55])
        result_batch = await rate_limiter.check(
            "api_key:abc123", endpoint="/api/batch"
        )

        assert result_qualify.allowed is True
        assert result_batch.allowed is True

    @pytest.mark.asyncio
    async def test_slowdown_warning(self, rate_limiter, mock_redis):
        """Should return slowdown warning near threshold."""
        pipe = AsyncMock()
        # 80% of limit (48/60) triggers slowdown
        pipe.execute = AsyncMock(return_value=[49, True, 11])
        mock_redis.pipeline.return_value.__aenter__.return_value = pipe

        result = await rate_limiter.check("api_key:abc123")

        assert result.allowed is True
        assert result.slowdown_warning is True

    @pytest.mark.asyncio
    async def test_reset_client_limits(self, rate_limiter, mock_redis):
        """Should reset rate limits for a specific client."""
        mock_redis.delete = AsyncMock(return_value=1)

        await rate_limiter.reset("api_key:abc123")

        mock_redis.delete.assert_called()

# ─── Middleware Tests ─────────────────────────────────────


class TestRateLimitMiddleware:
    """Tests for FastAPI rate limiting middleware."""

    @pytest.mark.asyncio
    async def test_adds_rate_limit_headers(self):
        """Should add X-RateLimit headers to responses."""
        mock_limiter = AsyncMock()
        mock_limiter.check.return_value = MagicMock(
            allowed=True,
            remaining=55,
            limit=60,
            reset_at=int(time.time()) + 30,
            retry_after=None,
            slowdown_warning=False,
        )

        middleware = RateLimitMiddleware(mock_limiter)
        request = MagicMock(spec=Request)
        request.client.host = "192.168.1.1"
        request.headers = {"x-api-key": "test_key"}
        request.url.path = "/api/qualify"

        response = MagicMock()
        response.headers = {}

        call_next = AsyncMock(return_value=response)
        result = await middleware.dispatch(request, call_next)

        assert "X-RateLimit-Limit" in result.headers
        assert "X-RateLimit-Remaining" in result.headers
        assert "X-RateLimit-Reset" in result.headers

    @pytest.mark.asyncio
    async def test_returns_429_when_exceeded(self):
        """Should return 429 Too Many Requests when limit exceeded."""
        mock_limiter = AsyncMock()
        mock_limiter.check.return_value = MagicMock(
            allowed=False,
            remaining=0,
            limit=60,
            reset_at=int(time.time()) + 30,
            retry_after=30,
            slowdown_warning=False,
        )

        middleware = RateLimitMiddleware(mock_limiter)
        request = MagicMock(spec=Request)
        request.client.host = "192.168.1.1"
        request.headers = {"x-api-key": "test_key"}
        request.url.path = "/api/qualify"

        call_next = AsyncMock()
        result = await middleware.dispatch(request, call_next)

        assert result.status_code == 429
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_health_endpoints(self):
        """Should not rate limit health check endpoints."""
        mock_limiter = AsyncMock()
        middleware = RateLimitMiddleware(mock_limiter)

        request = MagicMock(spec=Request)
        request.url.path = "/health"

        response = MagicMock()
        call_next = AsyncMock(return_value=response)

        result = await middleware.dispatch(request, call_next)

        mock_limiter.check.assert_not_called()
        assert result == response

# ─── Client Identifier Tests ─────────────────────────────


class TestClientIdentifier:
    """Tests for extracting client identifiers from requests."""

    def test_extract_from_api_key(self):
        """Should use API key as primary identifier."""
        request = MagicMock(spec=Request)
        request.headers = {"x-api-key": "sk_test_abc123"}
        request.client.host = "192.168.1.1"

        identifier = get_client_identifier(request)
        assert identifier == "api_key:sk_test_abc123"

    def test_extract_from_bearer_token(self):
        """Should fall back to Bearer token."""
        request = MagicMock(spec=Request)
        request.headers = {"authorization": "Bearer tok_xyz789"}
        request.client.host = "192.168.1.1"

        identifier = get_client_identifier(request)
        assert identifier == "bearer:tok_xyz789"

    def test_fallback_to_ip(self):
        """Should fall back to IP address when no auth present."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client.host = "192.168.1.1"

        identifier = get_client_identifier(request)
        assert identifier == "ip:192.168.1.1"

    def test_respects_x_forwarded_for(self):
        """Should use X-Forwarded-For when behind a proxy."""
        request = MagicMock(spec=Request)
        request.headers = {"x-forwarded-for": "10.0.0.1, 172.16.0.1"}
        request.client.host = "192.168.1.1"

        identifier = get_client_identifier(request)
        assert identifier == "ip:10.0.0.1"

    def test_api_key_takes_precedence(self):
        """API key should take precedence over IP."""
        request = MagicMock(spec=Request)
        request.headers = {
            "x-api-key": "sk_test_abc123",
            "x-forwarded-for": "10.0.0.1",
        }
        request.client.host = "192.168.1.1"

        identifier = get_client_identifier(request)
        assert identifier == "api_key:sk_test_abc123"


# ─── Config Tests ────────────────────────────────────────


class TestRateLimitConfig:
    """Tests for rate limit configuration."""

    def test_default_config(self):
        """Should have sensible defaults."""
        config = RateLimitConfig()
        assert config.requests_per_minute > 0
        assert config.requests_per_hour > config.requests_per_minute
        assert 0 < config.slowdown_threshold < 1

    def test_custom_config(self):
        """Should accept custom values."""
        config = RateLimitConfig(
            requests_per_minute=100,
            requests_per_hour=5000,
            burst_size=20,
            slowdown_threshold=0.9,
        )
        assert config.requests_per_minute == 100
        assert config.burst_size == 20

    def test_invalid_threshold_raises(self):
        """Should reject invalid slowdown thresholds."""
        with pytest.raises(ValueError):
            RateLimitConfig(slowdown_threshold=1.5)

    def test_negative_limits_raises(self):
        """Should reject negative rate limits."""
        with pytest.raises(ValueError):
            RateLimitConfig(requests_per_minute=-1)
