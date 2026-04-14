# mcp-manager

An MCP proxy server that wraps multiple MCP backends and exposes a **searchable tool catalog** through 5 lightweight tools. Instead of loading full tool schemas (8,000–20,000+ tokens) from every backend on each request, models can discover and call tools on demand.

## How It Works

```
LLM / MCP Client
      │
      ▼
┌───────────────────────────────────────────────┐
│          mcp-manager (proxy)                  │
│  search_tools  │  get_tool_schema             │
│  call_tool     │  list_servers                │
│  get_tools_by_server                         │
└───────────────┬─────────────────────────────┘
                │
     ┌──────────┼──────────┐
     ▼          ▼          ▼
  context7   github    filesystem
  (HTTP)    (stdio)     (stdio)
```

1. The model calls `search_tools` to find relevant tools by name/description
2. The model calls `get_tool_schema` to retrieve the full input schema for a specific tool
3. The model calls `call_tool` to execute the tool on the backend server
4. The model calls `get_tools_by_server` to get all tools available on a specific backend
5. Backend connections are spawned lazily and cached; idle connections are killed after 5 minutes

---

## Requirements

- Python 3.11+
- Node.js (for stdio backends using `npx`)

---

## Installation

```bash
# Clone and enter the project
git clone <repo-url>
cd mcp-manager

# Create a virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Edit `backends.json` to define your MCP backends:

```json
{
  "servers": {
    "my-http-server": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "timeout_seconds": 30
    },
    "my-stdio-server": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_TOKEN}"
      },
      "timeout_seconds": 30
    }
  }
}
```

### Backend types

| Field             | Type              | Description                                         |
| ----------------- | ----------------- | --------------------------------------------------- |
| `type`            | `http` \| `stdio` | Transport protocol                                  |
| `url`             | string            | HTTP endpoint (`http` type only)                    |
| `command`         | string            | Executable to spawn (`stdio` type only)             |
| `args`            | string[]          | Arguments for the command (`stdio` type only)       |
| `env`             | object            | Environment variables; supports `${env:VAR}` syntax |
| `timeout_seconds` | int               | Per-request timeout (default: 30)                   |

### Environment variable substitution

Values using `${env:VAR_NAME}` are replaced with the corresponding environment variable at startup:

```json
"env": {
  "GITHUB_PERSONAL_ACCESS_TOKEN": "${env:GITHUB_TOKEN}"
}
```

---

## Building the Catalog

Before starting the server, build the tool catalog. This queries all backends and snapshots their tool schemas into `catalog.json`:

```bash
python -m src.catalog.builder --config backends.json --output catalog.json
```

Or pass `--build-catalog` when starting the server to build it automatically:

```bash
python -m src.proxy.server --build-catalog
```

---

## Running the Server

### stdio mode (default — for MCP clients like Claude Desktop)

```bash
python -m src.proxy.server \
  --config backends.json \
  --catalog catalog.json \
  --transport stdio
```

### SSE/HTTP mode

```bash
python -m src.proxy.server \
  --config backends.json \
  --catalog catalog.json \
  --transport sse \
  --port 8000
```

### CLI options

| Option            | Default         | Description                        |
| ----------------- | --------------- | ---------------------------------- |
| `--config`        | `backends.json` | Path to backend configuration file |
| `--catalog`       | `catalog.json`  | Path to catalog file               |
| `--transport`     | `stdio`         | `stdio` or `sse`                   |
| `--port`          | `8000`          | Port for SSE mode                  |
| `--build-catalog` | false           | Build catalog before starting      |

Environment variable overrides: `MCP_PROXY_TRANSPORT`, `MCP_PROXY_PORT`.

---

## The 5 Proxy Tools

### `search_tools`

Fuzzy-search the catalog by tool name or description.

```json
{
  "query": "search for files",
  "max_results": 5
}
```

**Returns:**

```json
[
  {
    "server": "filesystem",
    "name": "search_files",
    "key": "filesystem/search_files",
    "description": "Search for files matching a pattern",
    "score": 0.91
  }
]
```

---

### `get_tool_schema`

Retrieve the full JSON Schema for a specific tool.

```json
{
  "server": "filesystem",
  "tool_name": "search_files"
}
```

**Returns:**

```json
{
  "success": true,
  "server": "filesystem",
  "tool_name": "search_files",
  "key": "filesystem/search_files",
  "description": "Search for files matching a pattern",
  "input_schema": { ... },
  "updated_at": "2026-03-26T10:00:00"
}
```

---

### `call_tool`

Execute a tool on a backend server.

```json
{
  "server": "filesystem",
  "tool_name": "search_files",
  "arguments": {
    "path": "/tmp",
    "pattern": "*.log"
  }
}
```

**Returns:**

```json
{
  "success": true,
  "result": [ ... ],
  "error": null,
  "elapsed_ms": 142
}
```

---

### `list_servers`

List all configured backends and their status.

**Returns:**

```json
[
  {
    "id": "filesystem",
    "name": "filesystem",
    "type": "stdio",
    "status": "ready",
    "tool_count": 5,
    "last_cataloged_at": "2026-03-26T10:00:00"
  }
]
```

---

### `get_tools_by_server`

Get all tools available for a specific backend server.

```json
{
  "server": "github"
}
```

**Returns:**

```json
[
  {
    "name": "search_repositories",
    "description": "Search for repositories on GitHub"
  },
  {
    "name": "get_user_profile",
    "description": "Get a user's GitHub profile information"
  }
]
```

---

## Claude Desktop Integration

Add mcp-manager to your Claude Desktop config (`~/.config/claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mcp-manager": {
      "command": "/path/to/venv/bin/python",
      "args": [
        "-m",
        "src.proxy.server",
        "--config",
        "/path/to/mcp-manager/backends.json",
        "--catalog",
        "/path/to/mcp-manager/catalog.json"
      ],
      "cwd": "/path/to/mcp-manager"
    }
  }
}
```

---

## Development

### Run tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/unit/test_search_tools.py -v
```

### Project structure

```
src/
  proxy/          # FastMCP server entrypoint
  catalog/        # Catalog schema (Pydantic) and builder
  backends/       # Connection manager (stdio & HTTP)
  tools/          # The 4 proxy tool implementations
tests/
  unit/           # Unit tests for each module
  integration/    # End-to-end proxy tests
  fixtures/       # Mock MCP server helpers
backends.json     # Backend configuration
catalog.json      # Generated tool catalog (auto-built)
```
