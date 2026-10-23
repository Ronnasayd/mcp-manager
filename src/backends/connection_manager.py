"""Connection manager: manages backend server lifecycle (stdio & HTTP)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

from src.catalog.schema import (
    BackendType,
    HttpBackendConfig,
    StdioBackendConfig,
)

logger = logging.getLogger(__name__)

ENV_VAR_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")

IDLE_TIMEOUT_SECONDS = 300  # 5 minutes


def _subst(value: str) -> str:
    return ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


class BackendConnection:
    """Abstract base for a backend connection."""

    def __init__(self, server_id: str) -> None:
        """Create a connection for the given backend server id."""
        self.server_id = server_id
        self._last_used: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _touch(self) -> None:
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        """Seconds elapsed since the connection was last used.

        Returns:
            float: Idle time in seconds.
        """
        return time.monotonic() - self._last_used

    async def call_tool(self, tool_name: str, arguments: dict) -> object:
        """Invoke a tool on the backend.

        Args:
            tool_name: Backend tool to invoke.
            arguments: Tool arguments.

        """
        raise NotImplementedError

    async def close(self) -> None:
        """Release backend resources."""


class StdioConnection(BackendConnection):
    """Manages a stdio subprocess MCP backend."""

    def __init__(self, server_id: str, cfg: StdioBackendConfig) -> None:
        """Create a lazily spawned stdio connection."""
        super().__init__(server_id)
        self._cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._initialized = False

    def is_running(self) -> bool:
        """Whether the backend process is spawned and still running.

        Returns:
            bool: True while the process is alive.
        """
        return self._proc is not None and self._proc.returncode is None

    def has_exited(self) -> bool:
        """Whether a previously spawned backend process has terminated.

        Returns:
            bool: True once the spawned process has exited.
        """
        return self._proc is not None and self._proc.returncode is not None

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
        # why: a respawned process is a new JSON-RPC session; ids restart at 1.
        self._req_id = 0
        await self._initialize()
        return self._proc

    async def _send(self, payload: dict) -> dict:
        proc = await self._ensure_process()
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError(f"[{self.server_id}] Process has no stdin/stdout pipes")
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
        if proc is None or proc.stdin is None:
            raise RuntimeError(f"[{self.server_id}] Process has no stdin pipe")
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

    async def call_tool(self, tool_name: str, arguments: dict) -> object:
        """Send a tools/call request over stdio.

        Args:
            tool_name: Backend tool to invoke.
            arguments: Tool arguments.

        Returns:
            object: The raw JSON-RPC result object.

        Raises:
            RuntimeError: When the backend answers with a JSON-RPC error.
        """
        async with self._lock:
            self._touch()
            # invariant: initialize must claim its request id before tools/call.
            await self._ensure_process()
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

        return resp.get("result", {"content": []})

    async def close(self) -> None:
        """Kill the backend process if it is running."""
        if self._proc and self._proc.returncode is None:
            logger.info("[%s] Closing stdio process", self.server_id)
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.kill()
                await self._proc.wait()
            # why: closing must never raise; any failure is only logged.
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.warning("[%s] Error closing process: %s", self.server_id, e)
        self._proc = None
        self._initialized = False


class HttpConnection(BackendConnection):
    """Manages an HTTP MCP backend."""

    def __init__(self, server_id: str, cfg: HttpBackendConfig) -> None:
        """Create an HTTP connection."""
        super().__init__(server_id)
        self._cfg = cfg
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def call_tool(self, tool_name: str, arguments: dict) -> object:
        """Send a tools/call request over HTTP, retrying 5xx responses.

        Args:
            tool_name: Backend tool to invoke.
            arguments: Tool arguments.

        Returns:
            object: The raw JSON-RPC result object.

        Raises:
            RuntimeError: When the backend answers with a JSON-RPC error.
        """
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

                return data.get("result", {"content": []})

            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code < 500:
                    break
            # why: any other failure is surfaced to the caller via last_exc.
            except Exception as e:  # pylint: disable=broad-exception-caught
                last_exc = e
                break

        raise last_exc or RuntimeError(f"[{self.server_id}] HTTP call failed")


class ConnectionManager:
    """Manages all backend connections with lazy spawn and idle timeout."""

    def __init__(self) -> None:
        """Create an empty connection manager."""
        self._connections: dict[str, BackendConnection] = {}
        self._configs: dict[str, StdioBackendConfig | HttpBackendConfig] = {}
        self._cleanup_task: asyncio.Task | None = None

    def register(
        self, server_id: str, cfg: StdioBackendConfig | HttpBackendConfig
    ) -> None:
        """Register a backend configuration under a server id.

        Args:
            server_id: Server id used to route calls.
            cfg: Backend configuration.
        """
        self._configs[server_id] = cfg

    def _get_or_create(self, server_id: str) -> BackendConnection:
        if server_id not in self._configs:
            raise KeyError(f"Unknown server: {server_id}")

        conn = self._connections.get(server_id)
        if conn is None or (isinstance(conn, StdioConnection) and conn.has_exited()):
            cfg = self._configs[server_id]
            if cfg.type == BackendType.STDIO:
                conn = StdioConnection(server_id, cfg)  # type: ignore[arg-type]
            else:
                conn = HttpConnection(server_id, cfg)  # type: ignore[arg-type]
            self._connections[server_id] = conn

        return conn

    async def call_tool(
        self, server_id: str, tool_name: str, arguments: dict
    ) -> object:
        """Route a tool call to the backend registered as server_id.

        Args:
            server_id: Target backend server id.
            tool_name: Backend tool to invoke.
            arguments: Tool arguments.

        Returns:
            object: The raw JSON-RPC result object.
        """
        conn = self._get_or_create(server_id)
        return await conn.call_tool(tool_name, arguments)

    def server_ids(self) -> list[str]:
        """List registered server ids.

        Returns:
            list[str]: Registered server ids.
        """
        return list(self._configs.keys())

    def is_alive(self, server_id: str) -> bool:
        """Whether the backend for server_id currently has a live connection.

        Args:
            server_id: Backend server id.

        Returns:
            bool: True for HTTP backends and running stdio processes.
        """
        conn = self._connections.get(server_id)
        if conn is None:
            return False
        if isinstance(conn, StdioConnection):
            return conn.is_running()
        return True  # HTTP is always considered alive

    async def start_cleanup_loop(self) -> None:
        """Start the background idle-connection cleanup task."""
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
        """Stop the cleanup task and close every connection."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for conn in self._connections.values():
            await conn.close()
        self._connections.clear()
