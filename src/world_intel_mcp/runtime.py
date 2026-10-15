"""Shared process-wide infrastructure for the MCP server.

Extracted from server.py in the Phase 26 modularization so that the
domain tool modules under ``tools/`` and the server shell can share one
cache, one circuit breaker, one AOI store, and one fetcher without a
circular import. Import-time side effects are deliberate and unchanged
from the original server.py module body: constructing this module opens
the cache database (honoring ``WORLD_INTEL_CACHE_DB``) exactly the way
the server always has, which is what the registry tests' temp-cache
override relies on.
"""

import logging

from .analysis import aoi
from .cache import Cache
from .circuit_breaker import CircuitBreaker
from .fetcher import Fetcher
from .diagnostics import exception_summary

logger = logging.getLogger("world-intel-mcp")

cache = Cache()
breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=300)
# Same physical SQLite file the Cache above resolved to (fallback path
# included); see analysis/aoi.py's module docstring for why AOIs live in
# a dedicated table there rather than as Cache entries.
_aoi_store = aoi.AOIStore(cache.db_path)
aoi_store = _aoi_store

# Vector store — optional, degrades gracefully if Qdrant unavailable.
_vector_store = None
try:
    from .vector_store import VectorStore, vector_dependencies_available

    if vector_dependencies_available():
        _vector_store = VectorStore(enabled=True)
    else:
        logger.info(
            "Vector store unavailable (qdrant_client / fastembed not installed)"
        )
except Exception as exc:
    logger.info("Vector store unavailable: %s", exception_summary(exc))

vector_store = _vector_store

fetcher = Fetcher(cache=cache, breaker=breaker, vector_store=_vector_store)
