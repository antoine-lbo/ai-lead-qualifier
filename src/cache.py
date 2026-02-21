"""
Redis Cache Layer for AI Lead Qualifier

Provides distributed caching with TTL management, cache invalidation,
key namespacing, and circuit breaker pattern for Redis failures.
Supports caching enrichment data, qualification scores, and API responses.
"""

import json
import hashlib
import logging
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional, Union
from functools import wraps

import redis
from redis.exceptions import ConnectionError, TimeoutError, RedisError

from src.config import settings

logger = logging.getLogger(__name__)


class CacheNamespace(str, Enum):
    """Cache key namespaces to prevent collisions."""
    ENRICHMENT = "enrichment"
    QUALIFICATION = "qualification"
    COMPANY = "company"
    RATE_LIMIT = "rate_limit"
    API_RESPONSE = "api_response"
    ANALYTICS = "analytics"


class CacheTTL:
    """Default TTL values in seconds for different cache types."""
    ENRICHMENT = 86400        # 24 hours — company data changes slowly
    QUALIFICATION = 3600      # 1 hour — scores may need refresh
    COMPANY_INFO = 604800     # 7 days — basic company info is stable
    API_RESPONSE = 300        # 5 minutes — external API responses
    RATE_LIMIT = 60           # 1 minute — rate limit windows
    ANALYTICS = 1800          # 30 minutes — aggregated metrics


class CircuitState(str, Enum):
    """Circuit breaker states for Redis connection."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Redis unavailable, skip cache
    HALF_OPEN = "half_open" # Testing if Redis recovered


class CacheStats:
    """Track cache performance metrics."""

    def __init__(self):
        self.hits = 0
        self.misses = 0
        self.errors = 0
        self.evictions = 0
        self._start_time = time.time()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total * 100) if total > 0 else 0.0

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "errors": self.errors,
            "evictions": self.evictions,
            "hit_rate_percent": round(self.hit_rate, 2),
            "uptime_seconds": round(self.uptime_seconds, 2),
        }

class CacheClient:
    """
    Redis cache client with circuit breaker pattern.

    Falls back gracefully when Redis is unavailable, ensuring
    the qualification pipeline continues without caching.
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        prefix: str = "lq",
        default_ttl: int = CacheTTL.QUALIFICATION,
        max_failures: int = 5,
        recovery_timeout: int = 30,
    ):
        self._prefix = prefix
        self._default_ttl = default_ttl
        self._stats = CacheStats()

        # Circuit breaker state
        self._circuit_state = CircuitState.CLOSED
        self._failure_count = 0
        self._max_failures = max_failures
        self._recovery_timeout = recovery_timeout
        self._last_failure_time: Optional[float] = None

        # Initialize Redis connection
        url = redis_url or getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
        try:
            self._redis = redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30,
            )
            self._redis.ping()
            logger.info("Redis cache connected: %s", url.split("@")[-1])
        except (ConnectionError, TimeoutError) as e:
            logger.warning("Redis unavailable, caching disabled: %s", e)
            self._redis = None
            self._circuit_state = CircuitState.OPEN

    def _build_key(self, namespace: CacheNamespace, key: str) -> str:
        """Build a namespaced cache key."""
        return f"{self._prefix}:{namespace.value}:{key}"

    def _hash_key(self, data: Union[str, dict]) -> str:
        """Generate a consistent hash for complex keys."""
        if isinstance(data, dict):
            data = json.dumps(data, sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def _check_circuit(self) -> bool:
        """Check if the circuit breaker allows operations."""
        if self._circuit_state == CircuitState.CLOSED:
            return True

        if self._circuit_state == CircuitState.OPEN:
            if (
                self._last_failure_time
                and time.time() - self._last_failure_time > self._recovery_timeout
            ):
                self._circuit_state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker half-open, testing Redis connection")
                return True
            return False

        # HALF_OPEN — allow one request through
        return True

    def _record_success(self):
        """Record a successful Redis operation."""
        if self._circuit_state == CircuitState.HALF_OPEN:
            self._circuit_state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info("Circuit breaker closed, Redis recovered")

    def _record_failure(self, error: Exception):
        """Record a failed Redis operation and potentially open circuit."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        self._stats.errors += 1

        if self._failure_count >= self._max_failures:
            self._circuit_state = CircuitState.OPEN
            logger.warning(
                "Circuit breaker opened after %d failures: %s",
                self._failure_count, error
            )
    # ── Core Operations ──────────────────────────────────────────

    def get(
        self, namespace: CacheNamespace, key: str
    ) -> Optional[Any]:
        """Retrieve a cached value by namespace and key."""
        if not self._redis or not self._check_circuit():
            self._stats.misses += 1
            return None

        full_key = self._build_key(namespace, key)
        try:
            raw = self._redis.get(full_key)
            self._record_success()

            if raw is None:
                self._stats.misses += 1
                return None

            self._stats.hits += 1
            return json.loads(raw)
        except (ConnectionError, TimeoutError) as e:
            self._record_failure(e)
            self._stats.misses += 1
            return None
        except json.JSONDecodeError:
            logger.warning("Corrupt cache entry: %s", full_key)
            self.delete(namespace, key)
            self._stats.misses += 1
            return None

    def set(
        self,
        namespace: CacheNamespace,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> bool:
        """Store a value in cache with optional TTL override."""
        if not self._redis or not self._check_circuit():
            return False

        full_key = self._build_key(namespace, key)
        ttl = ttl or self._default_ttl

        try:
            serialized = json.dumps(value, default=str)
            self._redis.setex(full_key, ttl, serialized)
            self._record_success()
            return True
        except (ConnectionError, TimeoutError) as e:
            self._record_failure(e)
            return False
        except (TypeError, ValueError) as e:
            logger.error("Failed to serialize cache value: %s", e)
            return False

    def delete(self, namespace: CacheNamespace, key: str) -> bool:
        """Remove a specific key from cache."""
        if not self._redis or not self._check_circuit():
            return False

        try:
            full_key = self._build_key(namespace, key)
            deleted = self._redis.delete(full_key)
            if deleted:
                self._stats.evictions += 1
            self._record_success()
            return bool(deleted)
        except (ConnectionError, TimeoutError) as e:
            self._record_failure(e)
            return False

    def invalidate_namespace(self, namespace: CacheNamespace) -> int:
        """Invalidate all keys within a namespace."""
        if not self._redis or not self._check_circuit():
            return 0

        pattern = f"{self._prefix}:{namespace.value}:*"
        try:
            keys = list(self._redis.scan_iter(match=pattern, count=100))
            if keys:
                deleted = self._redis.delete(*keys)
                self._stats.evictions += deleted
                logger.info("Invalidated %d keys in namespace %s", deleted, namespace.value)
                return deleted
            return 0
        except (ConnectionError, TimeoutError) as e:
            self._record_failure(e)
            return 0
    # ── Domain-Specific Helpers ──────────────────────────────────

    def get_enrichment(self, email: str) -> Optional[dict]:
        """Get cached enrichment data for a lead email."""
        key = self._hash_key(email.lower().strip())
        return self.get(CacheNamespace.ENRICHMENT, key)

    def set_enrichment(self, email: str, data: dict) -> bool:
        """Cache enrichment data for a lead email."""
        key = self._hash_key(email.lower().strip())
        return self.set(CacheNamespace.ENRICHMENT, key, data, ttl=CacheTTL.ENRICHMENT)

    def get_qualification(self, lead_id: str) -> Optional[dict]:
        """Get cached qualification score for a lead."""
        return self.get(CacheNamespace.QUALIFICATION, lead_id)

    def set_qualification(self, lead_id: str, result: dict) -> bool:
        """Cache qualification result for a lead."""
        return self.set(
            CacheNamespace.QUALIFICATION, lead_id, result, ttl=CacheTTL.QUALIFICATION
        )

    def get_company(self, domain: str) -> Optional[dict]:
        """Get cached company information by domain."""
        key = self._hash_key(domain.lower().strip())
        return self.get(CacheNamespace.COMPANY, key)

    def set_company(self, domain: str, data: dict) -> bool:
        """Cache company information by domain."""
        key = self._hash_key(domain.lower().strip())
        return self.set(CacheNamespace.COMPANY, key, data, ttl=CacheTTL.COMPANY_INFO)

    # ── Utilities ────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        """Get current cache performance statistics."""
        return {
            **self._stats.to_dict(),
            "circuit_state": self._circuit_state.value,
            "connected": self._redis is not None,
        }

    @property
    def is_available(self) -> bool:
        """Check if cache is currently available."""
        return self._redis is not None and self._check_circuit()

    def health_check(self) -> dict:
        """Perform a health check on the Redis connection."""
        status = {"status": "unhealthy", "latency_ms": None}

        if not self._redis:
            return status

        try:
            start = time.time()
            self._redis.ping()
            latency = (time.time() - start) * 1000
            status["status"] = "healthy"
            status["latency_ms"] = round(latency, 2)
            status["info"] = {
                "used_memory_human": self._redis.info("memory").get("used_memory_human"),
                "connected_clients": self._redis.info("clients").get("connected_clients"),
            }
        except RedisError as e:
            status["error"] = str(e)

        return status


def cached(
    namespace: CacheNamespace,
    ttl: Optional[int] = None,
    key_func: Optional[callable] = None,
):
    """
    Decorator for caching function results.

    Usage:
        @cached(CacheNamespace.ENRICHMENT, ttl=3600)
        async def enrich_lead(email: str) -> dict:
            ...
    """
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            cache_key = key_func(*args, **kwargs) if key_func else _auto_key(args, kwargs)
            cached_result = cache.get(namespace, cache_key)
            if cached_result is not None:
                logger.debug("Cache hit for %s:%s", namespace.value, cache_key[:8])
                return cached_result

            result = await func(*args, **kwargs)
            if result is not None:
                cache.set(namespace, cache_key, result, ttl=ttl)
            return result

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            cache_key = key_func(*args, **kwargs) if key_func else _auto_key(args, kwargs)
            cached_result = cache.get(namespace, cache_key)
            if cached_result is not None:
                logger.debug("Cache hit for %s:%s", namespace.value, cache_key[:8])
                return cached_result

            result = func(*args, **kwargs)
            if result is not None:
                cache.set(namespace, cache_key, result, ttl=ttl)
            return result

        import asyncio
        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
    return decorator


def _auto_key(args: tuple, kwargs: dict) -> str:
    """Generate a cache key from function arguments."""
    key_data = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    return hashlib.sha256(key_data.encode()).hexdigest()[:16]


# ── Module-Level Singleton ────────────────────────────────────────

cache = CacheClient()
