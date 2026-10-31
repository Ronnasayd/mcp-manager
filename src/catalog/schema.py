"""Pydantic models for the MCP proxy catalog."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# why: default asyncio StreamReader limit (64KiB) overflows on large stdio
# responses (big pseudocode/tool lists); shared by builder and connection_manager.
STDIO_READ_LIMIT = 10 * 1024 * 1024


class BackendType(StrEnum):
    """Backend transport kind: stdio subprocess or HTTP(S)."""

    STDIO = "stdio"
    HTTP = "http"
    HTTPS = "https"


class StdioBackendConfig(BaseModel):
    """Config for a backend spawned as a local stdio subprocess."""

    type: BackendType = BackendType.STDIO
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 300


class HttpBackendConfig(BaseModel):
    """Config for a backend reached over HTTP(S)."""

    type: BackendType
    url: str
    timeout_seconds: int = 300
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def must_be_http(cls, v: BackendType) -> BackendType:
        """Reject a stdio type on an HTTP backend config."""
        if v not in (BackendType.HTTP, BackendType.HTTPS):
            raise ValueError("type must be 'http' or 'https'")
        return v


class BackendsConfig(BaseModel):
    """Root config model for backends.json."""

    servers: dict[str, StdioBackendConfig | HttpBackendConfig]

    @model_validator(mode="before")
    @classmethod
    def coerce_server_types(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Coerce each raw server dict into its Stdio/Http config class by `type`."""
        servers = data.get("servers", {})
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                t = cfg.get("type", "stdio")
                if t == "stdio":
                    servers[name] = StdioBackendConfig(**cfg)
                elif t in ("http", "https"):
                    servers[name] = HttpBackendConfig(**cfg)
        return data


# ---------------------------------------------------------------------------
# Catalog models
# ---------------------------------------------------------------------------


class CatalogTool(BaseModel):
    """A single tool entry in the catalog."""

    server_id: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # Namespaced key: "{server_id}/{name}"
    key: str = ""

    @model_validator(mode="after")
    def set_key(self) -> CatalogTool:
        """Derive the namespaced key from server_id/name when not set."""
        if not self.key:
            self.key = f"{self.server_id}/{self.name}"
        return self


class CatalogBackend(BaseModel):
    """Backend entry in the catalog."""

    id: str
    name: str
    type: str
    tools: list[CatalogTool] = Field(default_factory=list)
    tool_count: int = 0
    last_cataloged_at: datetime = Field(default_factory=datetime.utcnow)
    error: str | None = None

    @model_validator(mode="after")
    def set_tool_count(self) -> CatalogBackend:
        """Keep tool_count in sync with the tools list length."""
        self.tool_count = len(self.tools)
        return self


class Catalog(BaseModel):
    """Root catalog model serialized to catalog.json."""

    version: str = "1.0"
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    backends: list[CatalogBackend] = Field(default_factory=list)

    def all_tools(self) -> list[CatalogTool]:
        """Flatten every backend's tools into a single list."""
        tools = []
        for backend in self.backends:
            tools.extend(backend.tools)
        return tools

    def find_tool(self, server_id: str, tool_name: str) -> CatalogTool | None:
        """Look up a tool by its server id and tool name, or None if absent."""
        backend = next((b for b in self.backends if b.id == server_id), None)
        if backend is None:
            return None
        return next((t for t in backend.tools if t.name == tool_name), None)
