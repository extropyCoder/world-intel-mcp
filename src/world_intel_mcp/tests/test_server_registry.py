"""Import-based tests for server.py's tool registry and dispatcher.

Every other test file reads server.py as *text* because importing it
opens a live ``Cache()``/``AOIStore()`` at the default on-disk path.
Here we import it for real, under a temp cache path
(``WORLD_INTEL_CACHE_DB`` is honored by ``Cache`` before the module-level
``cache = Cache()`` runs), which is what lets these tests verify the
registry structurally instead of by regex: the TOOLS/_dispatch parity
invariant, schema well-formedness, and the actual dispatch path for the
pure (no-network) tools.
"""

import importlib
import inspect
import os
import re
import tempfile
from pathlib import Path

import pytest

_TMP_CACHE = Path(tempfile.mkdtemp(prefix="wim-server-test-")) / "cache.db"


@pytest.fixture(scope="module")
def server():
    """Import server.py exactly once, cache pointed at a temp file."""
    prior = os.environ.get("WORLD_INTEL_CACHE_DB")
    os.environ["WORLD_INTEL_CACHE_DB"] = str(_TMP_CACHE)
    try:
        module = importlib.import_module("world_intel_mcp.server")
        yield module
    finally:
        if prior is None:
            os.environ.pop("WORLD_INTEL_CACHE_DB", None)
        else:
            os.environ["WORLD_INTEL_CACHE_DB"] = prior


def test_server_cache_landed_in_temp_path(server) -> None:
    """Guard for this file's own premise: if the env override stopped
    working, every test here would be silently exercising (and writing
    to) the developer's real cache."""
    assert Path(server.cache.db_path) == _TMP_CACHE


def test_tool_names_unique_and_conventional(server) -> None:
    names = [t.name for t in server.TOOLS]
    assert len(names) == len(set(names)), "duplicate tool name in TOOLS"
    non_conforming = [n for n in names if not n.startswith("intel_")]
    assert non_conforming == [], f"tools outside intel_* convention: {non_conforming}"


def test_every_tool_has_description_and_object_schema(server) -> None:
    for tool in server.TOOLS:
        assert tool.description and tool.description.strip(), tool.name
        assert isinstance(tool.inputSchema, dict), tool.name
        assert tool.inputSchema.get("type") == "object", tool.name


def test_tools_and_dispatch_are_in_one_to_one_parity(server) -> None:
    """The invariant ROADMAP.md carries ('TOOLS and _dispatch are
    aligned'). Since the Phase 26 split, dispatch is the union of the
    modular handler registry (tools.ALL_HANDLERS, whose per-module
    parity is enforced at import by tools.aggregate) and the shrinking
    legacy match/case; every registered tool must resolve through
    exactly that union, and nothing may be dispatchable without being
    registered."""
    from world_intel_mcp import tools as tools_pkg

    registered = {t.name for t in server.TOOLS}
    dispatch_src = inspect.getsource(server._dispatch)
    legacy_cases = set(re.findall(r'case "(intel_[a-z0-9_]+)"', dispatch_src))
    handled = legacy_cases | set(tools_pkg.ALL_HANDLERS)
    assert registered - handled == set(), "registered but never dispatched"
    assert handled - registered == set(), "dispatched but never registered"
    overlap = legacy_cases & set(tools_pkg.ALL_HANDLERS)
    assert overlap == set(), f"tool handled twice (legacy + modular): {overlap}"


def test_aggregate_refuses_drift_and_collisions() -> None:
    """The import-time guarantee has to be falsifiable: a module whose
    TOOLS and HANDLERS disagree, or that collides with an existing
    name, must refuse to aggregate."""
    import types

    from mcp.types import Tool as _Tool

    from world_intel_mcp import tools as tools_pkg

    drifty = types.SimpleNamespace(
        __name__="drifty",
        TOOLS=[
            _Tool(
                name="intel_fake",
                description="x",
                inputSchema={"type": "object", "properties": {}},
            )
        ],
        HANDLERS={},
    )
    with pytest.raises(RuntimeError, match="drift"):
        tools_pkg.aggregate([drifty])

    async def _h(arguments):
        return {}

    colliding = types.SimpleNamespace(
        __name__="colliding",
        TOOLS=[
            _Tool(
                name="intel_status",
                description="x",
                inputSchema={"type": "object", "properties": {}},
            )
        ],
        HANDLERS={"intel_status": _h},
    )
    from world_intel_mcp.tools import system as system_mod

    with pytest.raises(RuntimeError, match="collision"):
        tools_pkg.aggregate([system_mod, colliding])


def test_tool_count_matches_documented_surface(server) -> None:
    """139 = 138 intelligence tools + intel_status. If this fails after
    adding a tool, update README.md, ROADMAP.md, and this number in the
    same change: the repo's history shows doc counts drifting stale
    within weeks when nothing enforced them."""
    assert len(server.TOOLS) == 139


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error(server) -> None:
    result = await server._dispatch("intel_no_such_tool", {})
    assert result == {"error": "Unknown tool: intel_no_such_tool"}


@pytest.mark.asyncio
async def test_dispatch_reaches_real_aoi_store_round_trip(server) -> None:
    """Drive define -> list -> update -> delete through the real
    dispatcher and the module-level AOIStore (temp-backed): the tools
    exist end-to-end, not just as registry entries."""
    defined = await server._dispatch(
        "intel_aoi_define",
        {"name": "Registry Test", "lat": 40.44, "lon": -79.99, "radius_km": 25},
    )
    assert defined.get("aoi", {}).get("name") == "Registry Test"

    listed = await server._dispatch("intel_aoi_list", {})
    assert any(a["name"] == "Registry Test" for a in listed["aois"])

    updated = await server._dispatch(
        "intel_aoi_update", {"name": "Registry Test", "radius_km": 50}
    )
    assert updated.get("aoi", {}).get("radius_km") == 50.0

    deleted = await server._dispatch("intel_aoi_delete", {"name": "Registry Test"})
    assert deleted.get("deleted") == "Registry Test"


@pytest.mark.asyncio
async def test_dispatch_static_dataset_tool_works(server) -> None:
    """A pure static-config tool through the real dispatcher: no
    network, real data contract."""
    result = await server._dispatch("intel_military_bases", {})
    assert result.get("count", 0) >= 70
    assert any(
        b.get("name") == "Norfolk Naval Station" for b in result.get("bases", [])
    )


@pytest.mark.asyncio
async def test_dispatch_polygon_and_corridor_round_trip(server) -> None:
    """Shape AOIs exist end-to-end through the real dispatcher."""
    poly = await server._dispatch(
        "intel_aoi_define_polygon",
        {
            "name": "Registry Poly",
            "vertices": [[39.5, -81.0], [39.5, -79.0], [41.3, -79.0], [41.3, -81.0]],
        },
    )
    assert poly.get("aoi", {}).get("kind") == "polygon"

    corr = await server._dispatch(
        "intel_aoi_define_corridor",
        {
            "name": "Registry Corridor",
            "waypoints": [[40.0, -80.0], [41.0, -78.0]],
            "width_km": 40,
        },
    )
    assert corr.get("aoi", {}).get("kind") == "corridor"

    listed = await server._dispatch("intel_aoi_list", {})
    kinds = {a["name"]: a["kind"] for a in listed["aois"]}
    assert kinds.get("Registry Poly") == "polygon"
    assert kinds.get("Registry Corridor") == "corridor"

    for name in ("Registry Poly", "Registry Corridor"):
        deleted = await server._dispatch("intel_aoi_delete", {"name": name})
        assert deleted.get("deleted") == name


_REPO = Path(__file__).resolve().parents[3]


def test_readme_documents_every_registered_tool(server) -> None:
    """The README's per-tool reference had drifted to 126 of 135 tools by
    2026-10-11 while every headline count said 135. Pin the reference to
    the registry itself: same names, and each section's "(n)" matches its
    rows."""
    readme = (_REPO / "README.md").read_text()
    ref = readme.split("## MCP Tools Reference", 1)[1].split("\n## ", 1)[0]
    registered = {t.name for t in server.TOOLS}
    listed: list[str] = []
    for section in re.split(r"^### ", ref, flags=re.M)[1:]:
        header, _, body = section.partition("\n")
        rows = re.findall(r"^\| `(intel_[a-z0-9_]+)`", body, re.M)
        count = re.search(r"\((\d+)\)\s*$", header)
        assert count, f"README section without a (n) count: {header}"
        assert int(count.group(1)) == len(rows), f"README '{header}' says {count.group(1)}, lists {len(rows)}"
        listed.extend(rows)
    assert len(listed) == len(set(listed)), "a tool is listed twice in the README reference"
    assert set(listed) == registered, {
        "undocumented": sorted(registered - set(listed)),
        "documented_but_not_registered": sorted(set(listed) - registered),
    }
    n = len(server.TOOLS)
    assert f"**{n} MCP tools**" in readme
    assert f"**Total: {n} tools**" in readme


@pytest.mark.asyncio
async def test_call_tool_logs_redacted_copies_and_sanitizes_error_fields(server, monkeypatch, caplog):
    import copy
    import json
    import logging
    from unittest.mock import AsyncMock

    secrets = ["DUMMY_TOOL_API_KEY", "DUMMY_NESTED_TOKEN", "DUMMY_HEADER_PASSWORD",
               "DUMMY_URL_USER", "DUMMY_URL_PASSWORD", "DUMMY_URL_PATH_KEY"]
    url = f"https://{secrets[3]}:{secrets[4]}@source.test/api/{secrets[5]}?API%5fKEY={secrets[0]}"
    arguments = {"region": "europe", "api_key": secrets[0],
                 "nested": [{"Auth%6Frization": f"Bearer {secrets[1]}",
                             "credentials": {"password": secrets[2]}, "url": url}]}
    before = copy.deepcopy(arguments)
    result = {"error": {"message": f"failed {url}", "headers": {"Authorization": secrets[1]},
                        "api_key": {"value": secrets[0]}, "credentials": [{"password": secrets[2]}]},
              "nested": {"errors": [{"headers": [{"Auth%6Frization": secrets[1]}],
                                     "API%5fKEY": [secrets[0]], "message": "upstream failed"}]},
              "normal_data": {"value": 42, "api_key": "ordinary-data",
                              "headers": {"Authorization": "unchanged"}}}
    original_result = copy.deepcopy(result)
    dispatch = AsyncMock(return_value=result)
    monkeypatch.setattr(server, "_dispatch", dispatch)
    with caplog.at_level(logging.INFO):
        contents = await server.call_tool("intel_wildfires", arguments)
    rendered = caplog.text + contents[0].text
    for secret in secrets:
        assert secret not in rendered
    assert "intel_wildfires" in caplog.text and "europe" in caplog.text
    assert "REDACTED" in caplog.text
    dispatch.assert_awaited_once_with("intel_wildfires", arguments)
    assert dispatch.await_args.args[1] is arguments  # actual caller object unchanged
    assert arguments == before and result == original_result
    output = json.loads(contents[0].text)
    assert output["normal_data"] == original_result["normal_data"]
    assert output["error"]["headers"]["Authorization"] == "[REDACTED]"
    assert output["nested"]["errors"][0]["message"] == "upstream failed"


@pytest.mark.asyncio
async def test_call_tool_exception_does_not_render_credential_traceback(server, monkeypatch, caplog):
    import json
    import logging
    import httpx

    secret = "DUMMY_CHAINED_HTTP_SECRET"
    request = httpx.Request("GET", f"https://service.test/{secret}?api_key={secret}")
    response = httpx.Response(403, request=request)

    async def dispatch(*args):
        try:
            raise httpx.HTTPStatusError(secret, request=request, response=response)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError("tool operation failed") from exc

    monkeypatch.setattr(server, "_dispatch", dispatch)
    with caplog.at_level(logging.INFO):
        contents = await server.call_tool("intel_wildfires", {"api_key": secret})
    assert secret not in caplog.text + contents[0].text
    assert "RuntimeError" in caplog.text and "intel_wildfires" in caplog.text
    assert json.loads(contents[0].text)["error"] == "tool operation failed"
    assert all(record.exc_info is None for record in caplog.records)
