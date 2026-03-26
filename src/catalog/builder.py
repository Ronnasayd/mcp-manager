"""Catalog builder: discovers and snapshots backend tool schemas."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from src.catalog.schema import (
    BackendsConfig,
    BackendType,
    Catalog,
    CatalogBackend,
    CatalogTool,
)

logger = logging.getLogger(__name__)

ENV_VAR_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


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


async def _list_tools_stdio(server_id: str, cfg: Any, timeout: int) -> list[dict]:
    """Spawn a stdio MCP server and call list_tools."""
    import anyio

    cmd = [cfg.command] + cfg.args
    env = {**os.environ, **{k: substitute_env_vars(v) for k, v in cfg.env.items()}}

    tools: list[dict] = []
    try:
        async with anyio.from_thread.start_blocking_portal() as _:
            pass
    except Exception:
        pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
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

        proc.stdin.write(request.encode())
        await proc.stdin.drain()

        # Read initialize response
        async with asyncio.timeout(timeout):
            line = await proc.stdout.readline()
            if line:
                logger.debug(
                    "[%s] initialize response: %s", server_id, line.decode().strip()
                )

        # Send initialized notification
        notif = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
            + "\n"
        )
        proc.stdin.write(notif.encode())
        await proc.stdin.drain()

        # Call list_tools
        list_req = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
            )
            + "\n"
        )
        proc.stdin.write(list_req.encode())
        await proc.stdin.drain()

        async with asyncio.timeout(timeout):
            line = await proc.stdout.readline()
            if line:
                resp = json.loads(line.decode())
                tools = resp.get("result", {}).get("tools", [])

    except TimeoutError:
        logger.warning("[%s] Timed out listing tools", server_id)
    except Exception as e:
        logger.warning("[%s] Error listing tools: %s", server_id, e)
    finally:
        try:
            proc.stdin.close()
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    return tools


async def _list_tools_http(server_id: str, cfg: Any, timeout: int) -> list[dict]:
    """Call list_tools on an HTTP MCP server.

    Supports both plain JSON-RPC responses and MCP Streamable HTTP (SSE) responses.
    """
    import httpx

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
                # Parse SSE: extract the first JSON data line
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        data_str = line[len("data:") :].strip()
                        if data_str:
                            data = json.loads(data_str)
                            return data.get("result", {}).get("tools", [])
                return []
            else:
                data = resp.json()
                return data.get("result", {}).get("tools", [])
    except Exception as e:
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
            if cfg.type == BackendType.STDIO:
                raw_tools = await _list_tools_stdio(server_id, cfg, cfg.timeout_seconds)
            else:
                raw_tools = await _list_tools_http(server_id, cfg, cfg.timeout_seconds)
        except Exception as e:
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
            except Exception as e:
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
    return catalog


def main() -> None:
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
