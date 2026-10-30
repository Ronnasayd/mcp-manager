"""Unit tests for catalog builder stdio tool-listing."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.catalog.builder import _list_tools_stdio
from src.catalog.schema import BackendType, StdioBackendConfig


def _make_stdio_cfg() -> StdioBackendConfig:
    return StdioBackendConfig(type=BackendType.STDIO, command="fake-cmd", args=[])


def _make_fake_create_subprocess_exec(initialize_line: bytes, list_tools_line: bytes):
    """Build a fake asyncio.create_subprocess_exec whose returned proc.stdout is a
    real asyncio.StreamReader honoring the caller's `limit` kwarg, so the test
    reproduces the real LimitOverrunError for oversized lines when the limit is
    too small, and the real fix when it is large enough."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        limit = kwargs.get("limit", 65536)
        reader = asyncio.StreamReader(limit=limit)
        reader.feed_data(initialize_line + list_tools_line)
        reader.feed_eof()

        proc.stdout = reader
        return proc

    return fake_create_subprocess_exec


class TestListToolsStdio:
    """Tests for _list_tools_stdio against large tools/list responses."""

    @pytest.mark.asyncio
    async def test_handles_large_tools_list_response(self):
        """A tools/list response bigger than the default 64KB stdio limit must
        still be parsed successfully instead of raising LimitOverrunError."""
        big_description = "x" * 100_000
        tools_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "big_tool",
                        "description": big_description,
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        }
        list_line = (json.dumps(tools_payload) + "\n").encode()
        assert len(list_line) > 65536

        init_line = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n"
        ).encode()

        fake_exec = _make_fake_create_subprocess_exec(init_line, list_line)

        with patch("asyncio.create_subprocess_exec", fake_exec):
            tools = await _list_tools_stdio("rea", _make_stdio_cfg(), timeout=5)

        assert len(tools) == 1
        assert tools[0]["name"] == "big_tool"

    @pytest.mark.asyncio
    async def test_handles_small_tools_list_response(self):
        """A normal, well-under-the-limit response still parses correctly."""
        tools_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [{"name": "small_tool", "description": "", "inputSchema": {}}]
            },
        }
        list_line = (json.dumps(tools_payload) + "\n").encode()
        init_line = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n"
        ).encode()

        fake_exec = _make_fake_create_subprocess_exec(init_line, list_line)

        with patch("asyncio.create_subprocess_exec", fake_exec):
            tools = await _list_tools_stdio("rea", _make_stdio_cfg(), timeout=5)

        assert len(tools) == 1
        assert tools[0]["name"] == "small_tool"
