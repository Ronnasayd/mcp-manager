"""Unit tests for catalog Pydantic schema models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.catalog.schema import (
    BackendsConfig,
    Catalog,
    CatalogBackend,
    CatalogTool,
    HttpBackendConfig,
    StdioBackendConfig,
)


class TestBackendsConfig:
    def test_stdio_backend(self):
        raw = {
            "servers": {
                "github": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["@modelcontextprotocol/server-github"],
                    "env": {},
                }
            }
        }
        config = BackendsConfig.model_validate(raw)
        assert "github" in config.servers
        cfg = config.servers["github"]
        assert isinstance(cfg, StdioBackendConfig)
        assert cfg.command == "npx"

    def test_http_backend(self):
        raw = {
            "servers": {
                "context7": {
                    "type": "http",
                    "url": "https://mcp.context7.com/mcp",
                    "timeout_seconds": 30,
                }
            }
        }
        config = BackendsConfig.model_validate(raw)
        cfg = config.servers["context7"]
        assert isinstance(cfg, HttpBackendConfig)
        assert cfg.url == "https://mcp.context7.com/mcp"

    def test_missing_command_raises(self):
        raw = {"servers": {"bad": {"type": "stdio"}}}  # missing command
        with pytest.raises(ValidationError):
            BackendsConfig.model_validate(raw)

    def test_multiple_backends(self):
        raw = {
            "servers": {
                "a": {"type": "stdio", "command": "python", "args": ["a.py"]},
                "b": {"type": "http", "url": "http://localhost:9000"},
            }
        }
        config = BackendsConfig.model_validate(raw)
        assert len(config.servers) == 2


class TestCatalogTool:
    def test_key_auto_set(self):
        t = CatalogTool(server_id="myserver", name="my_tool", description="does stuff")
        assert t.key == "myserver/my_tool"

    def test_empty_description_allowed(self):
        t = CatalogTool(server_id="s", name="tool")
        assert t.description == ""

    def test_input_schema_defaults_empty(self):
        t = CatalogTool(server_id="s", name="tool")
        assert t.input_schema == {}


class TestCatalogBackend:
    def test_tool_count_auto_set(self):
        tools = [
            CatalogTool(server_id="s", name="t1"),
            CatalogTool(server_id="s", name="t2"),
        ]
        b = CatalogBackend(id="s", name="s", type="http", tools=tools)
        assert b.tool_count == 2

    def test_error_field(self):
        b = CatalogBackend(id="s", name="s", type="http", error="connection refused")
        assert b.error == "connection refused"


class TestCatalog:
    def test_all_tools_flat(self):
        t1 = CatalogTool(server_id="s1", name="tool_a")
        t2 = CatalogTool(server_id="s2", name="tool_b")
        catalog = Catalog(
            backends=[
                CatalogBackend(id="s1", name="s1", type="http", tools=[t1]),
                CatalogBackend(id="s2", name="s2", type="stdio", tools=[t2]),
            ]
        )
        all_tools = catalog.all_tools()
        assert len(all_tools) == 2

    def test_find_tool_found(self):
        t = CatalogTool(server_id="srv", name="my_tool", description="hello")
        catalog = Catalog(
            backends=[
                CatalogBackend(id="srv", name="srv", type="http", tools=[t]),
            ]
        )
        found = catalog.find_tool("srv", "my_tool")
        assert found is not None
        assert found.name == "my_tool"

    def test_find_tool_not_found(self):
        catalog = Catalog(backends=[])
        result = catalog.find_tool("no_server", "no_tool")
        assert result is None

    def test_serialization_round_trip(self):
        t = CatalogTool(
            server_id="s",
            name="tool",
            description="hi",
            input_schema={"type": "object"},
        )
        b = CatalogBackend(id="s", name="s", type="http", tools=[t])
        catalog = Catalog(backends=[b])
        json_str = catalog.model_dump_json()
        restored = Catalog.model_validate_json(json_str)
        assert restored.all_tools()[0].name == "tool"
