"""Connection manager: manages backend server lifecycle (stdio & HTTP)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from src.catalog.schema import BackendType, HttpBackendConfig, StdioBackendConfig

logger = logging.getLogger(__name__)

ENV_VAR_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")

IDLE_TIMEOUT_SECONDS = 300  # 5 minutes


def _subst(value: str) -> str:
    return ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


class BackendConnection:
    """Abstract base for a backend connection."""

    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self._last_used: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _touch(self) -> None:
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class StdioConnection(BackendConnection):
    """Manages a stdio subprocess MCP backend."""

    def __init__(self, server_id: str, cfg: StdioBackendConfig) -> None:
        super().__init__(server_id)
        self._cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._initialized = False

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc

        logger.info(
            "[%s] Spawning stdio process: %s %s",
            self.server_id,
            self._cfg.command,
            self._cfg.args,
        )
        cmd = [self._cfg.command] + [_subst(a) for a in self._cfg.args]
        env = {**os.environ, **{k: _subst(v) for k, v in self._cfg.env.items()}}

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._initialized = False
        await self._initialize()
        return self._proc

    async def _send(self, payload: dict) -> dict:
        proc = await self._ensure_process()
        request_id = payload.get("id")
        line = json.dumps(payload) + "\n"
        proc.stdin.write(line.encode())
        await proc.stdin.drain()

        async with asyncio.timeout(self._cfg.timeout_seconds):
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    raise RuntimeError(
                        f"[{self.server_id}] Process closed stdout unexpectedly"
                    )
                msg = json.loads(raw.decode())
                # Skip notifications and progress messages (no matching id)
                if "id" in msg and msg["id"] == request_id:
                    return msg
                logger.debug("[%s] Skipping server message: %s", self.server_id, msg)

    async def _initialize(self) -> None:
        if self._initialized:
            return
        resp = await self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-proxy", "version": "1.0"},
                },
            }
        )
        logger.debug("[%s] initialize response: %s", self.server_id, resp)

        proc = self._proc
        notif = (
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
            + "\n"
        )
        proc.stdin.write(notif.encode())
        await proc.stdin.drain()
        self._initialized = True

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        async with self._lock:
            self._touch()
            resp = await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }
            )
        if "error" in resp:
            raise RuntimeError(resp["error"].get("message", "Unknown error"))

        result = resp.get("result", {})
        content = result.get("content", result)
        return json.dumps(content) if isinstance(content, (dict, list)) else content

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            logger.info("[%s] Closing stdio process", self.server_id)
            try:
                self._proc.stdin.close()
                self._proc.kill()
                await self._proc.wait()
            except Exception as e:
                logger.warning("[%s] Error closing process: %s", self.server_id, e)
        self._proc = None
        self._initialized = False


class HttpConnection(BackendConnection):
    """Manages an HTTP MCP backend."""

    def __init__(self, server_id: str, cfg: HttpBackendConfig) -> None:
        super().__init__(server_id)
        self._cfg = cfg
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        import httpx

        self._touch()
        url = self._cfg.url.rstrip("/")
        headers = {"Content-Type": "application/json", **self._cfg.headers}
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=self._cfg.timeout_seconds
                ) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code >= 500 and attempt < 2:
                        wait = 2**attempt
                        logger.warning(
                            "[%s] HTTP %s, retrying in %ss",
                            self.server_id,
                            resp.status_code,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json()

                if "error" in data:
                    raise RuntimeError(data["error"].get("message", "Unknown error"))

                result = data.get("result", {})
                return result.get("content", result)

            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code < 500:
                    break
            except Exception as e:
                last_exc = e
                break

        raise last_exc or RuntimeError(f"[{self.server_id}] HTTP call failed")


class ConnectionManager:
    """Manages all backend connections with lazy spawn and idle timeout."""

    def __init__(self) -> None:
        self._connections: dict[str, BackendConnection] = {}
        self._configs: dict[str, StdioBackendConfig | HttpBackendConfig] = {}
        self._cleanup_task: asyncio.Task | None = None

    def register(
        self, server_id: str, cfg: StdioBackendConfig | HttpBackendConfig
    ) -> None:
        self._configs[server_id] = cfg

    def _get_or_create(self, server_id: str) -> BackendConnection:
        if server_id not in self._configs:
            raise KeyError(f"Unknown server: {server_id}")

        conn = self._connections.get(server_id)
        if conn is None or (
            isinstance(conn, StdioConnection)
            and conn._proc is not None
            and conn._proc.returncode is not None
        ):
            cfg = self._configs[server_id]
            if cfg.type == BackendType.STDIO:
                conn = StdioConnection(server_id, cfg)  # type: ignore[arg-type]
            else:
                conn = HttpConnection(server_id, cfg)  # type: ignore[arg-type]
            self._connections[server_id] = conn

        return conn

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict) -> Any:
        conn = self._get_or_create(server_id)
        return await conn.call_tool(tool_name, arguments)

    def server_ids(self) -> list[str]:
        return list(self._configs.keys())

    def is_alive(self, server_id: str) -> bool:
        conn = self._connections.get(server_id)
        if conn is None:
            return False
        if isinstance(conn, StdioConnection):
            return conn._proc is not None and conn._proc.returncode is None
        return True  # HTTP is always considered alive

    async def start_cleanup_loop(self) -> None:
        self._cleanup_task = asyncio.create_task(self._idle_cleanup_loop())

    async def _idle_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            for server_id, conn in list(self._connections.items()):
                if (
                    isinstance(conn, StdioConnection)
                    and conn.idle_seconds() > IDLE_TIMEOUT_SECONDS
                ):
                    logger.info("[%s] Idle timeout, closing connection", server_id)
                    await conn.close()
                    del self._connections[server_id]

    async def close_all(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for conn in self._connections.values():
            await conn.close()
        self._connections.clear()
