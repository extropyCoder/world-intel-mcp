"""Real Qdrant query API, fully offline: no service or embedding model needed.

Run with the declared minimum and current client separately. Only this module
skips when the optional Qdrant client is absent; mock-backed unit tests still run.
"""

import math
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("qdrant_client", reason="install the vector extra for real local query tests")

from qdrant_client import QdrantClient, models
from qdrant_client.http.models.models import QueryResponse

from world_intel_mcp import vector_store as vs


NOW = 2_000_000_000.0
VECTOR = [1.0] + [0.0] * (vs.EMBEDDING_DIM - 1)


def _point(point_id, score, domain, category, age, **extra):
    payload = {
        "domain": domain, "category": category, "timestamp": NOW - age,
        "text": f"point-{point_id}", "datetime": f"datetime-{point_id}", "has_error": False,
    }
    payload.update(extra)
    return models.PointStruct(
        id=point_id,
        vector=[score, math.sqrt(1 - score * score)] + [0.0] * (vs.EMBEDDING_DIM - 2),
        payload=payload,
    )


@pytest.fixture
def local_store(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("real query tests must not connect or download models")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(vs, "_get_embed_model", forbidden)
    monkeypatch.setattr(vs, "_embed_text", lambda _: list(VECTOR))
    monkeypatch.setattr(vs, "time", SimpleNamespace(time=lambda: NOW))
    # Queries are sequential; permit the existing asyncio.to_thread boundary.
    client = QdrantClient(":memory:", force_disable_check_same_thread=True)
    client.create_collection(
        collection_name=vs.COLLECTION_NAME,
        vectors_config=models.VectorParams(size=vs.EMBEDDING_DIM, distance=models.Distance.COSINE),
    )
    client.upsert(vs.COLLECTION_NAME, points=[
        _point(1, 1.0, "markets", "Financial Markets", 10, event_count=5, country="US"),
        _point(2, 0.8, "crypto", "Cryptocurrency", 20, country="JP"),
        _point(3, 0.6, "bonds", "Financial Markets", 3600),
        _point(4, 0.95, "markets", "Financial Markets", 3600.1),
        _point(5, 0.99, "news", "News & Media", 5, has_error=True),
        _point(6, 0.4, "markets", "Financial Markets", 30),
        _point(7, -1.0, "other", "Other", 40),
        models.PointStruct(id=8, vector=VECTOR, payload={"timestamp": NOW}),
        models.PointStruct(id=9, vector=VECTOR, payload=None),
    ])
    monkeypatch.setattr(vs, "_get_qdrant", lambda: client)
    # A real signature/implementation validates kwargs; the spy only observes.
    query = Mock(wraps=client.query_points)
    monkeypatch.setattr(client, "query_points", query)
    if hasattr(client, "search"):
        monkeypatch.setattr(client, "search", forbidden)
    try:
        yield vs.VectorStore(), client, query
    finally:
        client.close()


def test_real_query_response_envelope(local_store):
    _, client, _ = local_store
    response = client.query_points(
        collection_name=vs.COLLECTION_NAME, query=VECTOR, limit=1, with_payload=True,
        query_filter=models.Filter(must=[models.FieldCondition(
            key="has_error", match=models.MatchValue(value=False))]),
    )
    assert isinstance(response, QueryResponse)
    assert len(response.points) == 1 and response.points[0].payload["text"] == "point-1"
    assert response.points[0].score == pytest.approx(1.0)


@pytest.mark.parametrize("empty", [False, True])
async def test_real_collection_stats_preserve_available_and_unknown_counts(local_store, empty):
    store, client, _ = local_store
    if empty:
        client.delete_collection(vs.COLLECTION_NAME)
        client.create_collection(vs.COLLECTION_NAME, vectors_config=models.VectorParams(
            size=vs.EMBEDDING_DIM, distance=models.Distance.COSINE))
    info = client.get_collection(vs.COLLECTION_NAME)
    result = await store.collection_stats()
    assert result == {
        "enabled": True,
        "collection": vs.COLLECTION_NAME,
        "points_count": 0 if empty else 9,
        "vectors_count": getattr(info, "vectors_count", None),
        "indexed_vectors_count": info.indexed_vectors_count,
        "status": info.status.value if info.status else "unknown",
        "embedding_model": vs.EMBEDDING_MODEL,
        "embedding_dim": vs.EMBEDDING_DIM,
    }
    if not hasattr(info, "vectors_count"):
        assert result["vectors_count"] is None


async def test_semantic_filters_ranking_shape_and_limit(local_store):
    store, _, query = local_store
    result = await store.semantic_search("query", category="Financial Markets", hours=1, limit=2)
    assert "error" not in result
    assert result["filters"] == {"domain": None, "category": "Financial Markets", "hours": 1}
    assert result["count"] == 2
    assert [point["text"] for point in result["results"]] == ["point-1", "point-3"]
    assert [point["score"] for point in result["results"]] == [1.0, 0.6]
    assert result["results"][0] == {
        "score": 1.0, "domain": "markets", "category": "Financial Markets", "text": "point-1",
        "datetime": "datetime-1", "event_count": 5, "country": "US",
    }
    assert query.call_args.kwargs["limit"] == 2
    assert query.call_args.kwargs["with_payload"] is True
    assert query.call_args.kwargs["query"] == VECTOR
    assert "query_vector" not in query.call_args.kwargs
    assert "score_threshold" not in query.call_args.kwargs
    domain_result = await store.semantic_search("query", domain="markets", category="Financial Markets", hours=1)
    assert [p["text"] for p in domain_result["results"]] == ["point-1", "point-6"]


async def test_semantic_no_window_retains_old_and_negative_scores(local_store):
    store, _, _ = local_store
    result = await store.semantic_search("query")
    assert [point["text"] for point in result["results"]] == [
        "point-1", "point-4", "point-2", "point-3", "point-6", "point-7",
    ]
    assert result["results"][-1]["score"] == -1.0
    assert result["results"][-1]["event_count"] is None
    assert result["results"][-1]["country"] is None


async def test_similar_is_cross_domain_and_preserves_reference(local_store):
    store, _, query = local_store
    text = "x" * 300
    result = await store.find_similar("reference-only", text, limit=3, hours=1)
    assert result["reference_domain"] == "reference-only"
    assert result["reference_text"] == text[:200]
    assert [point["domain"] for point in result["similar"]] == ["markets", "crypto", "bonds"]
    assert [point["score"] for point in result["similar"]] == [1.0, 0.8, 0.6]
    assert result["count"] == 3 and result["similar"][1]["country"] == "JP"
    assert query.call_args.kwargs["limit"] == 3
    assert query.call_args.kwargs["with_payload"] is True


async def test_correlate_keeps_top_per_category_and_category_order(local_store):
    store, _, query = local_store
    result = await store.cross_domain_correlate("query", hours=1, limit_per_domain=1)
    assert result["query"] == "query" and result["hours"] == 1
    assert result["domains_found"] == 3 and result["total_signals"] == 3
    assert [c["category"] for c in result["correlations"]] == ["Financial Markets", "Cryptocurrency", "Other"]
    assert [c["best_score"] for c in result["correlations"]] == [1.0, 0.8, -1.0]
    assert all(c["signal_count"] == 1 for c in result["correlations"])
    assert result["correlations"][0]["signals"][0]["text"] == "point-1"
    assert query.call_args.kwargs["limit"] == 100
    assert query.call_args.kwargs["with_payload"] is True


@pytest.mark.parametrize("method,args,result_key", [
    ("semantic_search", ("query",), "results"),
    ("find_similar", ("reference", "text"), "similar"),
    ("cross_domain_correlate", ("query",), "correlations"),
])
async def test_real_empty_and_missing_collection_errors(local_store, method, args, result_key):
    store, client, _ = local_store
    # A tiny positive window excludes all valid points; missing payloads/error
    # points must never leak through has_error=False filtering.
    result = await getattr(store, method)(*args, hours=0.00001)
    assert "error" not in result and result[result_key] == []
    client.delete_collection(vs.COLLECTION_NAME)
    result = await getattr(store, method)(*args)
    assert "error" in result and result[result_key] == []
    assert result.get("count", result.get("total_signals")) == 0


async def test_real_empty_collection(local_store):
    store, client, _ = local_store
    client.delete_collection(vs.COLLECTION_NAME)
    client.create_collection(vs.COLLECTION_NAME, vectors_config=models.VectorParams(
        size=vs.EMBEDDING_DIM, distance=models.Distance.COSINE))
    result = await store.semantic_search("query")
    assert "error" not in result and result["count"] == 0 and result["results"] == []


async def test_missing_optional_payload_fields_keep_defaults(local_store):
    store, client, _ = local_store
    client.upsert(vs.COLLECTION_NAME, points=[models.PointStruct(
        id=6, vector=[0.4, math.sqrt(0.84)] + [0.0] * (vs.EMBEDDING_DIM - 2),
        payload={"has_error": False, "timestamp": NOW - 30},
    )])
    result = await store.semantic_search("query", hours=1)
    assert result["results"][-2] == {
        "score": 0.4, "domain": "", "category": "", "text": "", "datetime": "",
        "event_count": None, "country": None,
    }
    similar = await store.find_similar("reference", "query", hours=1)
    assert similar["similar"][-2]["text"] == ""
    correlations = await store.cross_domain_correlate("query", hours=1)
    other = next(c for c in correlations["correlations"] if c["category"] == "Other")
    assert other["best_score"] == 0.4 and other["signals"][0]["domain"] == ""
