"""
Tests for the Redis cache layer.

Uses fakeredis for isolated, in-memory testing without
requiring a running Redis instance.
"""

import json
import time
from unittest.mock import patch, MagicMock

import pytest
import fakeredis

from src.cache import (
    CacheClient,
    CacheNamespace,
    CacheTTL,
    CacheStats,
    CircuitState,
    cached,
    _auto_key,
)


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def fake_redis():
    """Create a fakeredis instance for testing."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def cache_client(fake_redis):
    """Create a CacheClient with a fake Redis backend."""
    client = CacheClient.__new__(CacheClient)
    client._prefix = "test"
    client._default_ttl = 300
    client._stats = CacheStats()
    client._circuit_state = CircuitState.CLOSED
    client._failure_count = 0
    client._max_failures = 5
    client._recovery_timeout = 30
    client._last_failure_time = None
    client._redis = fake_redis
    return client


# ── Key Building ─────────────────────────────────────────────────


class TestKeyBuilding:
    def test_build_key_format(self, cache_client):
        key = cache_client._build_key(CacheNamespace.ENRICHMENT, "abc123")
        assert key == "test:enrichment:abc123"

    def test_build_key_different_namespaces(self, cache_client):
        k1 = cache_client._build_key(CacheNamespace.ENRICHMENT, "x")
        k2 = cache_client._build_key(CacheNamespace.QUALIFICATION, "x")
        assert k1 != k2

    def test_hash_key_string(self, cache_client):
        h1 = cache_client._hash_key("test@example.com")
        h2 = cache_client._hash_key("test@example.com")
        assert h1 == h2
        assert len(h1) == 16

    def test_hash_key_dict(self, cache_client):
        h1 = cache_client._hash_key({"a": 1, "b": 2})
        h2 = cache_client._hash_key({"b": 2, "a": 1})
        assert h1 == h2  # sort_keys ensures consistency


# ── Core Operations ──────────────────────────────────────────────


class TestCoreOperations:
    def test_set_and_get(self, cache_client):
        data = {"company": "Acme Corp", "score": 87}
        cache_client.set(CacheNamespace.QUALIFICATION, "lead1", data)
        result = cache_client.get(CacheNamespace.QUALIFICATION, "lead1")
        assert result == data

    def test_get_missing_key(self, cache_client):
        result = cache_client.get(CacheNamespace.ENRICHMENT, "nonexistent")
        assert result is None

    def test_set_with_custom_ttl(self, cache_client, fake_redis):
        cache_client.set(CacheNamespace.API_RESPONSE, "resp1", {"ok": True}, ttl=60)
        ttl = fake_redis.ttl("test:api_response:resp1")
        assert 0 < ttl <= 60

    def test_delete_existing_key(self, cache_client):
        cache_client.set(CacheNamespace.ENRICHMENT, "del_me", {"x": 1})
        assert cache_client.delete(CacheNamespace.ENRICHMENT, "del_me") is True
        assert cache_client.get(CacheNamespace.ENRICHMENT, "del_me") is None

    def test_delete_nonexistent_key(self, cache_client):
        assert cache_client.delete(CacheNamespace.ENRICHMENT, "nope") is False

    def test_invalidate_namespace(self, cache_client):
        for i in range(5):
            cache_client.set(CacheNamespace.ENRICHMENT, f"key{i}", {"i": i})
        cache_client.set(CacheNamespace.QUALIFICATION, "keep_me", {"safe": True})

        deleted = cache_client.invalidate_namespace(CacheNamespace.ENRICHMENT)
        assert deleted == 5
        assert cache_client.get(CacheNamespace.QUALIFICATION, "keep_me") == {"safe": True}

# ── Circuit Breaker ──────────────────────────────────────────────


class TestCircuitBreaker:
    def test_closed_allows_operations(self, cache_client):
        assert cache_client._check_circuit() is True

    def test_open_blocks_operations(self, cache_client):
        cache_client._circuit_state = CircuitState.OPEN
        cache_client._last_failure_time = time.time()
        assert cache_client._check_circuit() is False

    def test_open_transitions_to_half_open(self, cache_client):
        cache_client._circuit_state = CircuitState.OPEN
        cache_client._last_failure_time = time.time() - 60  # Past recovery timeout
        assert cache_client._check_circuit() is True
        assert cache_client._circuit_state == CircuitState.HALF_OPEN

    def test_half_open_recovers_on_success(self, cache_client):
        cache_client._circuit_state = CircuitState.HALF_OPEN
        cache_client._record_success()
        assert cache_client._circuit_state == CircuitState.CLOSED
        assert cache_client._failure_count == 0

    def test_failures_open_circuit(self, cache_client):
        for i in range(5):
            cache_client._record_failure(ConnectionError("down"))
        assert cache_client._circuit_state == CircuitState.OPEN

    def test_get_returns_none_when_circuit_open(self, cache_client):
        cache_client.set(CacheNamespace.ENRICHMENT, "key1", {"data": True})
        cache_client._circuit_state = CircuitState.OPEN
        cache_client._last_failure_time = time.time()
        assert cache_client.get(CacheNamespace.ENRICHMENT, "key1") is None


# ── Domain Helpers ───────────────────────────────────────────────


class TestDomainHelpers:
    def test_enrichment_roundtrip(self, cache_client):
        data = {"company_size": "50-200", "industry": "Technology"}
        cache_client.set_enrichment("john@acme.com", data)
        assert cache_client.get_enrichment("john@acme.com") == data

    def test_enrichment_case_insensitive(self, cache_client):
        data = {"industry": "Finance"}
        cache_client.set_enrichment("John@Acme.COM", data)
        assert cache_client.get_enrichment("john@acme.com") == data

    def test_qualification_roundtrip(self, cache_client):
        result = {"score": 92, "tier": "HOT", "reasoning": "Strong fit"}
        cache_client.set_qualification("lead_abc", result)
        assert cache_client.get_qualification("lead_abc") == result

    def test_company_roundtrip(self, cache_client):
        info = {"name": "Acme Corp", "employees": 500, "revenue": "$50M"}
        cache_client.set_company("acme.com", info)
        assert cache_client.get_company("acme.com") == info

    def test_company_case_insensitive(self, cache_client):
        info = {"name": "Test"}
        cache_client.set_company("ACME.COM", info)
        assert cache_client.get_company("acme.com") == info


# ── Stats & Health ───────────────────────────────────────────────


class TestStatsAndHealth:
    def test_stats_tracking(self, cache_client):
        cache_client.set(CacheNamespace.ENRICHMENT, "k1", {"x": 1})
        cache_client.get(CacheNamespace.ENRICHMENT, "k1")  # hit
        cache_client.get(CacheNamespace.ENRICHMENT, "miss")  # miss

        stats = cache_client.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate_percent"] == 50.0
        assert stats["connected"] is True
        assert stats["circuit_state"] == "closed"

    def test_is_available(self, cache_client):
        assert cache_client.is_available is True

    def test_is_not_available_when_no_redis(self, cache_client):
        cache_client._redis = None
        assert cache_client.is_available is False

    def test_cache_stats_to_dict(self):
        stats = CacheStats()
        stats.hits = 10
        stats.misses = 5
        d = stats.to_dict()
        assert d["hits"] == 10
        assert d["hit_rate_percent"] == 66.67


# ── Auto Key Generation ──────────────────────────────────────────


class TestAutoKey:
    def test_consistent_keys(self):
        k1 = _auto_key(("a", "b"), {"c": 1})
        k2 = _auto_key(("a", "b"), {"c": 1})
        assert k1 == k2

    def test_different_args_different_keys(self):
        k1 = _auto_key(("a",), {})
        k2 = _auto_key(("b",), {})
        assert k1 != k2


# ── Graceful Degradation ─────────────────────────────────────────


class TestGracefulDegradation:
    def test_set_returns_false_without_redis(self, cache_client):
        cache_client._redis = None
        assert cache_client.set(CacheNamespace.ENRICHMENT, "k", {"x": 1}) is False

    def test_get_returns_none_without_redis(self, cache_client):
        cache_client._redis = None
        assert cache_client.get(CacheNamespace.ENRICHMENT, "k") is None

    def test_delete_returns_false_without_redis(self, cache_client):
        cache_client._redis = None
        assert cache_client.delete(CacheNamespace.ENRICHMENT, "k") is False

    def test_invalidate_returns_zero_without_redis(self, cache_client):
        cache_client._redis = None
        assert cache_client.invalidate_namespace(CacheNamespace.ENRICHMENT) == 0
