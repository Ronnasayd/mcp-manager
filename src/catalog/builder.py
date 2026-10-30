"""Catalog builder: discovers and snapshots backend tool schemas."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re

import httpx
from pydantic import ValidationError

from src.catalog.schema import (
    BackendsConfig,
    Catalog,
    CatalogBackend,
    CatalogTool,
    HttpBackendConfig,
    StdioBackendConfig,
)
from src.catalog.vector_store import build_index

logger = logging.getLogger(__name__)

ENV_VAR_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")

# Default asyncio StreamReader limit (64KB) is too small for backends whose
# tools/list response (many tools / large schemas) exceeds it in one line.
STDIO_READ_LIMIT = 10 * 1024 * 1024


def substitute_env_vars(value: str) -> str:
    """Substitute ${VAR} or ${env:VAR} placeholders with environment values."""

    def replace(m: re.Match) -> str:
        var = m.group(1)
        result = os.environ.get(var, "")
        if not result:
            logger.warning("Environment variable '%s' not set", var)
        return result

    return ENV_VAR_RE.sub(replace, value)


def resolve_config(config: BackendsConfig) -> BackendsConfig:
    """Resolve env var placeholders throughout config values."""
    raw = config.model_dump()
    resolved_str = substitute_env_vars(json.dumps(raw))
    return BackendsConfig.model_validate_json(resolved_str)


async def _mcp_handshake_and_list_tools(
    server_id: str,
    stdin: asyncio.StreamWriter,
    stdout: asyncio.StreamReader,
    timeout: int,
) -> list[dict]:
    """Run the MCP stdio handshake and return the discovered tools.

    Sends initialize/initialized/tools-list over an already-open pipe pair.
    """
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "catalog-builder", "version": "1.0"},
                },
            }
        )
        + "\n"
    )
    stdin.write(request.encode())
    await stdin.drain()

    async with asyncio.timeout(timeout):
        line = await stdout.readline()
        if line:
            logger.debug(
                "[%s] initialize response: %s", server_id, line.decode().strip()
            )

    notif = (
        json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        + "\n"
    )
    stdin.write(notif.encode())
    await stdin.drain()

    list_req = (
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        + "\n"
    )
    stdin.write(list_req.encode())
    await stdin.drain()

    async with asyncio.timeout(timeout):
        line = await stdout.readline()
        if line:
            resp = json.loads(line.decode())
            return resp.get("result", {}).get("tools", [])

    return []


async def _list_tools_stdio(
    server_id: str, cfg: StdioBackendConfig, timeout: int
) -> list[dict]:
    """Spawn a stdio MCP server and call list_tools."""
    cmd = [cfg.command, *cfg.args]
    env = {**os.environ, **{k: substitute_env_vars(v) for k, v in cfg.env.items()}}

    tools: list[dict] = []

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=STDIO_READ_LIMIT,
    )

    if proc.stdin is None or proc.stdout is None:
        logger.warning("[%s] Subprocess has no stdin/stdout pipes", server_id)
        return tools

    try:
        tools = await _mcp_handshake_and_list_tools(
            server_id, proc.stdin, proc.stdout, timeout
        )
    except TimeoutError:
        logger.warning("[%s] Timed out listing tools", server_id)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning("[%s] Error listing tools: %s", server_id, e)
    finally:
        await _terminate_process(server_id, proc)

    return tools


async def _terminate_process(server_id: str, proc: asyncio.subprocess.Process) -> None:
    """Close stdin and kill the subprocess, logging but not raising on failure."""
    try:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.kill()
        await proc.wait()
    except OSError as e:
        logger.debug("[%s] Error terminating subprocess: %s", server_id, e)


def _first_sse_data_line(text: str) -> str | None:
    """Return the payload of the first `data:` line in an SSE response, if any."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data_str = line[len("data:") :].strip()
            if data_str:
                return data_str
    return None


async def _list_tools_http(
    server_id: str, cfg: HttpBackendConfig, timeout: int
) -> list[dict]:
    """Call list_tools on an HTTP MCP server.

    Supports both plain JSON-RPC responses and MCP Streamable HTTP (SSE) responses.
    """
    url = cfg.url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        # MCP Streamable HTTP spec requires this Accept header
        "Accept": "application/json, text/event-stream",
        **cfg.headers,
    }

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                data_str = _first_sse_data_line(resp.text)
                data = json.loads(data_str) if data_str else {}
            else:
                data = resp.json()
            return data.get("result", {}).get("tools", [])
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.warning("[%s] HTTP error listing tools: %s", server_id, e)
        return []


async def build_catalog(config_path: Path, catalog_path: Path) -> Catalog:
    """Build a fresh catalog from all configured backends."""
    raw = json.loads(config_path.read_text())
    config = BackendsConfig.model_validate(raw)
    config = resolve_config(config)

    backends: list[CatalogBackend] = []

    for server_id, cfg in config.servers.items():
        logger.info("Cataloging backend: %s (type=%s)", server_id, cfg.type)
        raw_tools: list[dict] = []
        error: str | None = None

        try:
            if isinstance(cfg, StdioBackendConfig):
                raw_tools = await _list_tools_stdio(server_id, cfg, cfg.timeout_seconds)
            else:
                raw_tools = await _list_tools_http(server_id, cfg, cfg.timeout_seconds)
        except (OSError, ValueError, TimeoutError) as e:
            error = str(e)
            logger.error("[%s] Failed to catalog: %s", server_id, e)

        catalog_tools: list[CatalogTool] = []
        for t in raw_tools:
            try:
                catalog_tools.append(
                    CatalogTool(
                        server_id=server_id,
                        name=t.get("name", ""),
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {}),
                    )
                )
            except ValidationError as e:
                logger.warning(
                    "[%s] Skipping malformed tool %s: %s", server_id, t.get("name"), e
                )

        backends.append(
            CatalogBackend(
                id=server_id,
                name=server_id,
                type=cfg.type.value,
                tools=catalog_tools,
                error=error,
            )
        )
        logger.info("[%s] Cataloged %d tools", server_id, len(catalog_tools))

    catalog = Catalog(backends=backends)
    catalog_path.write_text(catalog.model_dump_json(indent=2))
    logger.info(
        "Catalog written to %s (%d total tools)", catalog_path, len(catalog.all_tools())
    )

    db_path = catalog_path.with_suffix(".db")
    build_index(catalog, db_path)

    return catalog


def main() -> None:
    """CLI entrypoint: parse args and build the catalog."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Build MCP proxy tool catalog")
    parser.add_argument(
        "--config", default="src/config/backends.json", help="Path to backends.json"
    )
    parser.add_argument(
        "--catalog", default="catalog.json", help="Output catalog.json path"
    )
    args = parser.parse_args()

    asyncio.run(build_catalog(Path(args.config), Path(args.catalog)))


if __name__ == "__main__":
    main()
