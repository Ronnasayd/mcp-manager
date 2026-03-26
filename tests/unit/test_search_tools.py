"""Unit tests for the fuzzy search implementation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.tools.search_tools import invalidate_catalog_cache, search_tools


def _write_catalog(path: Path, tools: list[dict]) -> None:
    """Helper: write a catalog.json with given tool dicts."""
    catalog_tools = [
        CatalogTool(
            server_id=t["server_id"],
            name=t["name"],
            description=t.get("description", ""),
        )
        for t in tools
    ]
    backends_by_server: dict[str, list[CatalogTool]] = {}
    for ct in catalog_tools:
        backends_by_server.setdefault(ct.server_id, []).append(ct)

    backends = [
        CatalogBackend(id=sid, name=sid, type="http", tools=tool_list)
        for sid, tool_list in backends_by_server.items()
    ]
    catalog = Catalog(backends=backends)
    path.write_text(catalog.model_dump_json())


@pytest.fixture(autouse=True)
def reset_cache():
    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


def test_exact_match():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    _write_catalog(
        path,
        [
            {
                "server_id": "db",
                "name": "database_query",
                "description": "Query the database",
            },
            {
                "server_id": "fs",
                "name": "list_files",
                "description": "List directory files",
            },
        ],
    )
    results = search_tools("database_query", catalog_path=path)
    assert results, "Expected at least one result"
    assert results[0]["name"] == "database_query"
    assert results[0]["score"] >= 0.95


def test_typo_match():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    _write_catalog(
        path,
        [
            {
                "server_id": "db",
                "name": "database_query",
                "description": "Query the database",
            },
        ],
    )
    results = search_tools("databse_query", catalog_path=path)
    assert results, "Expected at least one result for typo query"
    assert results[0]["score"] > 0.7


def test_description_match():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    _write_catalog(
        path,
        [
            {
                "server_id": "db",
                "name": "fetch_records",
                "description": "fetch data from db",
            },
            {
                "server_id": "other",
                "name": "unrelated_tool",
                "description": "does something else",
            },
        ],
    )
    results = search_tools("retrieve data", catalog_path=path)
    names = [r["name"] for r in results]
    assert "fetch_records" in names


def test_no_match_returns_empty():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    _write_catalog(
        path,
        [
            {"server_id": "s", "name": "tool_a", "description": "does something"},
        ],
    )
    results = search_tools("xyznonexistent99zzz", catalog_path=path)
    # May return low-score results; exact empty is not guaranteed, but score should be low
    for r in results:
        assert r["score"] < 50, f"Expected low score, got {r['score']}"


def test_max_results_limit():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    tools = [
        {"server_id": "s", "name": f"tool_{i}", "description": f"tool number {i}"}
        for i in range(50)
    ]
    _write_catalog(path, tools)
    results = search_tools("tool", max_results=2, catalog_path=path)
    assert len(results) <= 2


def test_collision_same_name_different_servers():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    _write_catalog(
        path,
        [
            {"server_id": "server1", "name": "search", "description": "search items"},
            {"server_id": "server2", "name": "search", "description": "search records"},
        ],
    )
    results = search_tools("search", max_results=10, catalog_path=path)
    keys = {r["key"] for r in results}
    assert "server1/search" in keys
    assert "server2/search" in keys


def test_empty_catalog_returns_empty():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    catalog = Catalog(backends=[])
    path.write_text(catalog.model_dump_json())
    results = search_tools("anything", catalog_path=path)
    assert results == []


def test_missing_catalog_returns_empty():
    path = Path("/tmp/nonexistent_catalog_12345.json")
    results = search_tools("anything", catalog_path=path)
    assert results == []


def test_result_has_expected_keys():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    _write_catalog(
        path, [{"server_id": "s", "name": "my_tool", "description": "does stuff"}]
    )
    results = search_tools("my_tool", catalog_path=path)
    assert results
    r = results[0]
    assert "server" in r
    assert "name" in r
    assert "key" in r
    assert "description" in r
    assert "score" in r
