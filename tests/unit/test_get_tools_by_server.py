"""Unit tests for get_tools_by_server."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.tools.get_tools_by_server import get_tools_by_server
from src.tools.search_tools import invalidate_catalog_cache


def _write_catalog(path: Path, servers: dict[str, list[dict]]) -> None:
    """Helper: write a catalog.json with given servers and tools."""
    backends = []
    for server_id, tools_list in servers.items():
        catalog_tools = [
            CatalogTool(
                server_id=server_id,
                name=t["name"],
                description=t.get("description", ""),
            )
            for t in tools_list
        ]
        backend = CatalogBackend(
            id=server_id,
            name=server_id,
            type="http",
            tools=catalog_tools,
        )
        backends.append(backend)

    catalog = Catalog(backends=backends)
    path.write_text(catalog.model_dump_json())


@pytest.fixture(autouse=True)
def reset_cache():
    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


def test_get_tools_by_server_with_valid_server():
    """Test retrieving tools for a valid server."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    try:
        _write_catalog(
            path,
            {
                "github": [
                    {"name": "create_repo", "description": "Create a GitHub repo"},
                    {"name": "list_issues", "description": "List issues"},
                ],
                "context7": [
                    {"name": "search_web", "description": "Search the web"},
                ],
            },
        )

        result = get_tools_by_server("github", catalog_path=path)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "create_repo"
        assert result[0]["description"] == "Create a GitHub repo"
        assert result[1]["name"] == "list_issues"
        assert result[1]["description"] == "List issues"
    finally:
        path.unlink()


def test_get_tools_by_server_with_invalid_server():
    """Test retrieving tools for a non-existent server returns empty list."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    try:
        _write_catalog(
            path,
            {
                "github": [
                    {"name": "create_repo", "description": "Create a GitHub repo"},
                ],
            },
        )

        result = get_tools_by_server("nonexistent", catalog_path=path)

        assert isinstance(result, list)
        assert len(result) == 0
    finally:
        path.unlink()


def test_get_tools_by_server_empty_server():
    """Test retrieving tools for a server with no tools."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    try:
        _write_catalog(
            path,
            {
                "github": [],
            },
        )

        result = get_tools_by_server("github", catalog_path=path)

        assert isinstance(result, list)
        assert len(result) == 0
    finally:
        path.unlink()


def test_get_tools_by_server_with_missing_catalog():
    """Test retrieving tools when catalog file doesn't exist."""
    result = get_tools_by_server("github", catalog_path=Path("/nonexistent/path"))

    assert isinstance(result, list)
    assert len(result) == 0


def test_get_tools_by_server_response_format():
    """Test that the response format contains only name and description."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    try:
        _write_catalog(
            path,
            {
                "github": [
                    {
                        "name": "create_repo",
                        "description": "Create a GitHub repo",
                    },
                ],
            },
        )

        result = get_tools_by_server("github", catalog_path=path)

        assert len(result) == 1
        tool = result[0]
        # Verify response contains only name and description
        assert set(tool.keys()) == {"name", "description"}
    finally:
        path.unlink()
