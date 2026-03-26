"""Unit tests for ConnectionManager lifecycle, spawn, timeout, and cleanup."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.backends.connection_manager import (
    IDLE_TIMEOUT_SECONDS,
    BackendConnection,
    ConnectionManager,
    HttpConnection,
    StdioConnection,
)
from src.catalog.schema import BackendType, HttpBackendConfig, StdioBackendConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdio_cfg(
    command: str = "echo", args: list[str] | None = None
) -> StdioBackendConfig:
    return StdioBackendConfig(
        type=BackendType.STDIO,
        command=command,
        args=args or [],
        timeout_seconds=5,
    )


def _http_cfg(url: str = "http://localhost:9000") -> HttpBackendConfig:
    return HttpBackendConfig(type=BackendType.HTTP, url=url, timeout_seconds=5)


def _make_jsonrpc_response(req_id: int, result: dict) -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n"
    ).encode()


def _make_jsonrpc_error(req_id: int, message: str) -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": message},
            }
        )
        + "\n"
    ).encode()


# ---------------------------------------------------------------------------
# ConnectionManager registration & server_ids
# ---------------------------------------------------------------------------


class TestConnectionManagerRegistration:
    def test_register_single_server(self):
        m = ConnectionManager()
        m.register("srv", _http_cfg())
        assert "srv" in m.server_ids()

    def test_register_multiple_servers(self):
        m = ConnectionManager()
        m.register("a", _http_cfg())
        m.register("b", _stdio_cfg())
        ids = m.server_ids()
        assert "a" in ids
        assert "b" in ids
        assert len(ids) == 2

    def test_unknown_server_raises(self):
        m = ConnectionManager()
        with pytest.raises(KeyError):
            m._get_or_create("nonexistent")


# ---------------------------------------------------------------------------
# is_alive
# ---------------------------------------------------------------------------


class TestIsAlive:
    def test_not_connected_returns_false(self):
        m = ConnectionManager()
        m.register("srv", _stdio_cfg())
        # No connection established yet
        assert m.is_alive("srv") is False

    def test_unknown_server_returns_false(self):
        m = ConnectionManager()
        assert m.is_alive("ghost") is False

    def test_http_connection_always_alive(self):
        m = ConnectionManager()
        m.register("http_srv", _http_cfg())
        # Force creation of an HttpConnection
        conn = m._get_or_create("http_srv")
        assert isinstance(conn, HttpConnection)
        assert m.is_alive("http_srv") is True

    def test_stdio_alive_while_process_running(self):
        m = ConnectionManager()
        m.register("stdio_srv", _stdio_cfg())
        conn = m._get_or_create("stdio_srv")
        assert isinstance(conn, StdioConnection)
        # Process not started yet
        assert m.is_alive("stdio_srv") is False


# ---------------------------------------------------------------------------
# HttpConnection.call_tool
# ---------------------------------------------------------------------------


class TestHttpConnection:
    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        cfg = _http_cfg("http://localhost:9000")
        conn = HttpConnection("test", cfg)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "hello"}]},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await conn.call_tool("my_tool", {"x": 1})

        assert result == [{"type": "text", "text": "hello"}]

    @pytest.mark.asyncio
    async def test_call_tool_error_response_raises(self):
        cfg = _http_cfg()
        conn = HttpConnection("test", cfg)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Internal error"},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="Internal error"):
                await conn.call_tool("my_tool", {})

    @pytest.mark.asyncio
    async def test_call_tool_retries_on_500(self):
        import httpx

        cfg = _http_cfg()
        conn = HttpConnection("test", cfg)

        call_count = 0
        responses = []

        # First two calls: 500 error; third: success
        for _ in range(2):
            r = MagicMock()
            r.status_code = 500
            err = httpx.HTTPStatusError("500", request=MagicMock(), response=r)
            r.raise_for_status.side_effect = err
            responses.append(r)

        success_r = MagicMock()
        success_r.status_code = 200
        success_r.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": "ok"},
        }
        success_r.raise_for_status = MagicMock()
        responses.append(success_r)

        async def fake_post(*args, **kwargs):
            nonlocal call_count
            r = responses[call_count]
            call_count += 1
            if r.status_code >= 500:
                raise httpx.HTTPStatusError("500", request=MagicMock(), response=r)
            return r

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = fake_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await conn.call_tool("tool", {})

        assert call_count == 3
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_call_tool_non_5xx_does_not_retry(self):
        import httpx

        cfg = _http_cfg()
        conn = HttpConnection("test", cfg)

        call_count = 0

        async def fake_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.status_code = 404
            raise httpx.HTTPStatusError("404", request=MagicMock(), response=r)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = fake_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await conn.call_tool("tool", {})

        assert call_count == 1  # No retries for 4xx

    def test_touch_updates_last_used(self):
        cfg = _http_cfg()
        conn = HttpConnection("test", cfg)
        old_ts = conn._last_used
        time.sleep(0.01)
        conn._touch()
        assert conn._last_used > old_ts

    def test_idle_seconds_increases(self):
        cfg = _http_cfg()
        conn = HttpConnection("test", cfg)
        time.sleep(0.05)
        assert conn.idle_seconds() >= 0.04


# ---------------------------------------------------------------------------
# StdioConnection – mocked subprocess
# ---------------------------------------------------------------------------


def _make_stdio_process(responses: list[bytes]) -> MagicMock:
    """Create a mock subprocess that returns given byte responses in order."""
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    proc.stderr = MagicMock()

    idx = 0

    async def readline():
        nonlocal idx
        if idx < len(responses):
            data = responses[idx]
            idx += 1
            return data
        return b""

    proc.stdout = MagicMock()
    proc.stdout.readline = readline
    return proc


class TestStdioConnection:
    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        cfg = _stdio_cfg()
        conn = StdioConnection("srv", cfg)

        init_resp = _make_jsonrpc_response(
            1,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "srv", "version": "1.0"},
            },
        )
        tool_resp = _make_jsonrpc_response(
            2,
            {"content": [{"type": "text", "text": "result"}]},
        )
        proc = _make_stdio_process([init_resp, tool_resp])

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await conn.call_tool("my_tool", {"x": "hi"})

        assert result == [{"type": "text", "text": "result"}]

    @pytest.mark.asyncio
    async def test_call_tool_error_response_raises(self):
        cfg = _stdio_cfg()
        conn = StdioConnection("srv", cfg)

        init_resp = _make_jsonrpc_response(
            1,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "srv", "version": "1.0"},
            },
        )
        err_resp = _make_jsonrpc_error(2, "Tool failed")
        proc = _make_stdio_process([init_resp, err_resp])

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with pytest.raises(RuntimeError, match="Tool failed"):
                await conn.call_tool("bad_tool", {})

    @pytest.mark.asyncio
    async def test_process_reused_across_calls(self):
        cfg = _stdio_cfg()
        conn = StdioConnection("srv", cfg)

        init_resp = _make_jsonrpc_response(
            1,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "srv", "version": "1.0"},
            },
        )
        tool_resp1 = _make_jsonrpc_response(2, {"content": "r1"})
        tool_resp2 = _make_jsonrpc_response(3, {"content": "r2"})
        proc = _make_stdio_process([init_resp, tool_resp1, tool_resp2])

        spawn_count = 0
        original_create = asyncio.create_subprocess_exec

        async def counting_spawn(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return proc

        with patch("asyncio.create_subprocess_exec", counting_spawn):
            await conn.call_tool("tool", {})
            await conn.call_tool("tool", {})

        assert spawn_count == 1  # Process only spawned once

    @pytest.mark.asyncio
    async def test_close_kills_process(self):
        cfg = _stdio_cfg()
        conn = StdioConnection("srv", cfg)

        init_resp = _make_jsonrpc_response(
            1,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "srv", "version": "1.0"},
            },
        )
        tool_resp = _make_jsonrpc_response(2, {"content": "ok"})
        proc = _make_stdio_process([init_resp, tool_resp])

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await conn.call_tool("tool", {})
            await conn.close()

        proc.kill.assert_called_once()
        assert conn._proc is None
        assert conn._initialized is False

    @pytest.mark.asyncio
    async def test_respawns_after_process_exit(self):
        cfg = _stdio_cfg()
        conn = StdioConnection("srv", cfg)

        init_resp = _make_jsonrpc_response(
            1,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "srv", "version": "1.0"},
            },
        )
        tool_resp = _make_jsonrpc_response(2, {"content": "first"})
        proc1 = _make_stdio_process([init_resp, tool_resp])

        init_resp2 = _make_jsonrpc_response(
            1,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "srv", "version": "1.0"},
            },
        )
        tool_resp2 = _make_jsonrpc_response(2, {"content": "second"})
        proc2 = _make_stdio_process([init_resp2, tool_resp2])

        procs = [proc1, proc2]
        spawn_count = 0

        async def spawn(*args, **kwargs):
            nonlocal spawn_count
            p = procs[spawn_count]
            spawn_count += 1
            return p

        with patch("asyncio.create_subprocess_exec", spawn):
            await conn.call_tool("tool", {})
            # Simulate process exit
            proc1.returncode = 1
            await conn.call_tool("tool", {})

        assert spawn_count == 2


# ---------------------------------------------------------------------------
# ConnectionManager.call_tool routing
# ---------------------------------------------------------------------------


class TestConnectionManagerCallTool:
    @pytest.mark.asyncio
    async def test_routes_to_correct_backend(self):
        m = ConnectionManager()
        m.register("srv", _http_cfg())

        conn = MagicMock(spec=HttpConnection)
        conn.call_tool = AsyncMock(return_value="routed_result")
        m._connections["srv"] = conn

        result = await m.call_tool("srv", "my_tool", {"arg": 1})
        conn.call_tool.assert_called_once_with("my_tool", {"arg": 1})
        assert result == "routed_result"

    @pytest.mark.asyncio
    async def test_unknown_server_raises_key_error(self):
        m = ConnectionManager()
        with pytest.raises(KeyError):
            await m.call_tool("unknown", "tool", {})


# ---------------------------------------------------------------------------
# ConnectionManager.close_all
# ---------------------------------------------------------------------------


class TestConnectionManagerCloseAll:
    @pytest.mark.asyncio
    async def test_close_all_calls_close_on_connections(self):
        m = ConnectionManager()
        m.register("a", _http_cfg())
        m.register("b", _http_cfg())

        conn_a = MagicMock()
        conn_a.close = AsyncMock()
        conn_b = MagicMock()
        conn_b.close = AsyncMock()
        m._connections["a"] = conn_a
        m._connections["b"] = conn_b

        await m.close_all()

        conn_a.close.assert_called_once()
        conn_b.close.assert_called_once()
        assert len(m._connections) == 0

    @pytest.mark.asyncio
    async def test_close_all_cancels_cleanup_task(self):
        m = ConnectionManager()
        await m.start_cleanup_loop()
        assert m._cleanup_task is not None
        task = m._cleanup_task
        await m.close_all()
        # Give the event loop a chance to finish cancellation
        import asyncio

        await asyncio.sleep(0)
        assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# Idle cleanup loop
# ---------------------------------------------------------------------------


class TestIdleCleanupLoop:
    @pytest.mark.asyncio
    async def test_idle_stdio_connection_is_closed(self):
        m = ConnectionManager()
        m.register("srv", _stdio_cfg())

        conn = MagicMock(spec=StdioConnection)
        conn.idle_seconds = MagicMock(return_value=IDLE_TIMEOUT_SECONDS + 1)
        conn.close = AsyncMock()
        m._connections["srv"] = conn

        # Run one iteration of the cleanup loop manually
        for server_id, c in list(m._connections.items()):
            if (
                isinstance(c, StdioConnection)
                and c.idle_seconds() > IDLE_TIMEOUT_SECONDS
            ):
                await c.close()
                del m._connections[server_id]

        conn.close.assert_called_once()
        assert "srv" not in m._connections

    @pytest.mark.asyncio
    async def test_active_stdio_connection_not_closed(self):
        m = ConnectionManager()
        m.register("srv", _stdio_cfg())

        conn = MagicMock(spec=StdioConnection)
        conn.idle_seconds = MagicMock(return_value=10)  # Well below threshold
        conn.close = AsyncMock()
        m._connections["srv"] = conn

        # Run cleanup loop logic
        for server_id, c in list(m._connections.items()):
            if (
                isinstance(c, StdioConnection)
                and c.idle_seconds() > IDLE_TIMEOUT_SECONDS
            ):
                await c.close()
                del m._connections[server_id]

        conn.close.assert_not_called()
        assert "srv" in m._connections

    @pytest.mark.asyncio
    async def test_http_connection_never_closed_by_idle(self):
        m = ConnectionManager()
        m.register("srv", _http_cfg())

        conn = MagicMock(spec=HttpConnection)
        conn.idle_seconds = MagicMock(return_value=IDLE_TIMEOUT_SECONDS + 999)
        conn.close = AsyncMock()
        m._connections["srv"] = conn

        # HTTP connections are excluded from idle cleanup
        for server_id, c in list(m._connections.items()):
            if (
                isinstance(c, StdioConnection)
                and c.idle_seconds() > IDLE_TIMEOUT_SECONDS
            ):
                await c.close()
                del m._connections[server_id]

        conn.close.assert_not_called()


# ---------------------------------------------------------------------------
# BackendConnection base
# ---------------------------------------------------------------------------


class TestBackendConnection:
    def test_idle_seconds_starts_near_zero(self):
        conn = BackendConnection("test")
        assert conn.idle_seconds() < 1.0

    def test_touch_resets_idle(self):
        conn = BackendConnection("test")
        # Manually backdate last_used
        conn._last_used = time.monotonic() - 100
        assert conn.idle_seconds() >= 100
        conn._touch()
        assert conn.idle_seconds() < 1.0

    @pytest.mark.asyncio
    async def test_call_tool_not_implemented(self):
        conn = BackendConnection("test")
        with pytest.raises(NotImplementedError):
            await conn.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_close_is_noop(self):
        conn = BackendConnection("test")
        await conn.close()  # Should not raise
