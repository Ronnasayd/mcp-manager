"""Pydantic models for the MCP proxy catalog."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class BackendType(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    HTTPS = "https"


class StdioBackendConfig(BaseModel):
    type: BackendType = BackendType.STDIO
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 300


class HttpBackendConfig(BaseModel):
    type: BackendType
    url: str
    timeout_seconds: int = 300
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def must_be_http(cls, v: BackendType) -> BackendType:
        if v not in (BackendType.HTTP, BackendType.HTTPS):
            raise ValueError("type must be 'http' or 'https'")
        return v


class BackendsConfig(BaseModel):
    """Root config model for backends.json."""

    servers: dict[str, StdioBackendConfig | HttpBackendConfig]

    @model_validator(mode="before")
    @classmethod
    def coerce_server_types(cls, data: Any) -> Any:
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
    def set_key(self) -> "CatalogTool":
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
    def set_tool_count(self) -> "CatalogBackend":
        self.tool_count = len(self.tools)
        return self


class Catalog(BaseModel):
    """Root catalog model serialized to catalog.json."""

    version: str = "1.0"
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    backends: list[CatalogBackend] = Field(default_factory=list)

    def all_tools(self) -> list[CatalogTool]:
        tools = []
        for backend in self.backends:
            tools.extend(backend.tools)
        return tools

    def find_tool(self, server_id: str, tool_name: str) -> CatalogTool | None:
        for backend in self.backends:
            if backend.id == server_id:
                for tool in backend.tools:
                    if tool.name == tool_name:
                        return tool
        return None
