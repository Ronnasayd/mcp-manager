"""End-to-end proxy integration tests with mock MCP servers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.backends.connection_manager import ConnectionManager
from src.catalog.schema import Catalog, CatalogBackend, CatalogTool
from src.tools.call_tool import call_tool
from src.tools.get_tool_schema import get_tool_schema
from src.tools.get_tools_by_server import get_tools_by_server
from src.tools.list_servers import list_servers
from src.tools.search_tools import invalidate_catalog_cache, search_tools


def _build_test_catalog(path: Path) -> None:
    """Create a catalog with two mock backends for testing."""
    tools_a = [
        CatalogTool(
            server_id="server_a",
            name="search_records",
            description="Search database records",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        ),
        CatalogTool(
            server_id="server_a",
            name="create_record",
            description="Create a new database record",
            input_schema={
                "type": "object",
                "required": ["data"],
                "properties": {"data": {"type": "object"}},
            },
        ),
    ]
    tools_b = [
        CatalogTool(
            server_id="server_b",
            name="list_files",
            description="List files in a directory",
        ),
        CatalogTool(
            server_id="server_b",
            name="read_file",
            description="Read file contents",
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
        ),
        CatalogTool(
            server_id="server_b",
            name="write_file",
            description="Write content to a file",
        ),
    ]
    catalog = Catalog(
        backends=[
            CatalogBackend(id="server_a", name="server_a", type="http", tools=tools_a),
            CatalogBackend(id="server_b", name="server_b", type="stdio", tools=tools_b),
        ]
    )
    path.write_text(catalog.model_dump_json())


@pytest.fixture(autouse=True)
def reset_cache():
    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    p = tmp_path / "catalog.json"
    _build_test_catalog(p)
    return p


@pytest.fixture
def manager() -> ConnectionManager:
    from src.catalog.schema import BackendType, HttpBackendConfig, StdioBackendConfig

    m = ConnectionManager()
    m.register(
        "server_a",
        HttpBackendConfig(type=BackendType.HTTP, url="http://localhost:9999"),
    )
    m.register(
        "server_b",
        StdioBackendConfig(type=BackendType.STDIO, command="echo", args=["hi"]),
    )
    return m


class TestSearchTools:
    def test_finds_records_tool(self, catalog_path):
        results = search_tools("search records", catalog_path=catalog_path)
        names = [r["name"] for r in results]
        assert "search_records" in names

    def test_finds_file_tools(self, catalog_path):
        results = search_tools("file", catalog_path=catalog_path)
        names = [r["name"] for r in results]
        assert any("file" in n for n in names)

    def test_returns_correct_server(self, catalog_path):
        results = search_tools("search_records", catalog_path=catalog_path)
        r = next(r for r in results if r["name"] == "search_records")
        assert r["server"] == "server_a"

    def test_max_results_respected(self, catalog_path):
        results = search_tools("record", max_results=1, catalog_path=catalog_path)
        assert len(results) == 1


class TestGetToolSchema:
    def test_returns_schema(self, catalog_path):
        result = get_tool_schema(
            "server_a", "search_records", catalog_path=catalog_path
        )
        assert result["success"] is True
        assert result["input_schema"]["type"] == "object"
        assert "query" in result["input_schema"]["properties"]

    def test_not_found(self, catalog_path):
        result = get_tool_schema("server_a", "nonexistent", catalog_path=catalog_path)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_wrong_server(self, catalog_path):
        result = get_tool_schema(
            "bad_server", "search_records", catalog_path=catalog_path
        )
        assert result["success"] is False

    def test_returns_updated_at(self, catalog_path):
        result = get_tool_schema("server_b", "list_files", catalog_path=catalog_path)
        assert result["success"] is True
        assert result["updated_at"] is not None


class TestListServers:
    def test_lists_all_servers(self, manager, catalog_path):
        result = list_servers(manager, catalog_path=catalog_path)
        ids = [r["id"] for r in result]
        assert "server_a" in ids
        assert "server_b" in ids

    def test_includes_tool_count(self, manager, catalog_path):
        result = list_servers(manager, catalog_path=catalog_path)
        a = next(r for r in result if r["id"] == "server_a")
        assert a["tool_count"] == 2

    def test_status_field_present(self, manager, catalog_path):
        result = list_servers(manager, catalog_path=catalog_path)
        for r in result:
            assert r["status"] in ("ready", "initializing", "error")


class TestGetToolsByServer:
    def test_returns_tools_for_valid_server(self, catalog_path):
        result = get_tools_by_server("server_a", catalog_path=catalog_path)
        assert isinstance(result, list)
        assert len(result) == 2
        names = [t["name"] for t in result]
        assert "search_records" in names
        assert "create_record" in names

    def test_returns_empty_list_for_invalid_server(self, catalog_path):
        result = get_tools_by_server("nonexistent", catalog_path=catalog_path)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_response_includes_name_and_description(self, catalog_path):
        result = get_tools_by_server("server_b", catalog_path=catalog_path)
        assert len(result) > 0
        for tool in result:
            assert "name" in tool
            assert "description" in tool
            assert isinstance(tool["name"], str)
            assert isinstance(tool["description"], str)

    def test_correct_tool_count(self, catalog_path):
        result_a = get_tools_by_server("server_a", catalog_path=catalog_path)
        result_b = get_tools_by_server("server_b", catalog_path=catalog_path)
        assert len(result_a) == 2
        assert len(result_b) == 3


class TestFullWorkflow:
    @pytest.mark.asyncio
    async def test_search_then_call(self, manager, catalog_path):
        """Full workflow: search → get schema → call tool."""
        # Step 1: search
        search_results = search_tools("search records", catalog_path=catalog_path)
        assert search_results

        top = search_results[0]
        server = top["server"]
        tool_name = top["name"]

        # Step 2: get schema
        schema_result = get_tool_schema(server, tool_name, catalog_path=catalog_path)
        assert schema_result["success"] is True

        # Step 3: call tool (mock the backend)
        manager.call_tool = AsyncMock(return_value={"rows": []})
        result = await call_tool(
            server,
            tool_name,
            {"query": "hello"},
            manager,
            catalog_path=catalog_path,
        )
        assert result["success"] is True
        assert "elapsed_ms" in result

    @pytest.mark.asyncio
    async def test_concurrent_calls(self, manager, catalog_path):
        """Concurrent calls to same backend should all succeed."""
        import asyncio

        manager.call_tool = AsyncMock(return_value="ok")

        tasks = [
            call_tool(
                "server_a",
                "search_records",
                {"query": f"q{i}"},
                manager,
                catalog_path=catalog_path,
            )
            for i in range(10)
        ]
        results = await asyncio.gather(*tasks)
        assert all(r["success"] for r in results)

    @pytest.mark.asyncio
    async def test_error_propagation(self, manager, catalog_path):
        """Backend errors are wrapped and returned cleanly."""
        manager.call_tool = AsyncMock(side_effect=RuntimeError("connection refused"))
        result = await call_tool(
            "server_a",
            "search_records",
            {"query": "x"},
            manager,
            catalog_path=catalog_path,
        )
        assert result["success"] is False
        assert "connection refused" in result["error"]
