#!/usr/bin/env python3
"""SessionStart/UserPromptSubmit hook: injects mcp-manager usage context.

Usage: mcp_manager_context.py <path-to-catalog.json>
"""

import json
import sys


def build_context(catalog_path: str | None) -> str:
    """Build the mcp-manager explanation and server table for hook output.

    Args:
        catalog_path: Path to catalog.json produced by generate.sh, or None.

    Returns:
        Markdown text explaining mcp-manager and listing available servers.
    """
    lines = [
        "# mcp-manager",
        "",
        "mcp-manager is a proxy in front of several MCP backend servers. "
        "It exposes 4 tools: `list_servers`, `search_tools`, `get_tool_schema`, `call_tool`.",
        "",
        "Flow to use any backend tool:",
        "1. Find the tool: `search_tools` (fuzzy match by keyword) or `list_servers` to browse by backend.",
        "2. Load its schema: `get_tool_schema(server, tool_name)` — ALWAYS do this before calling, "
        "arguments must match the returned inputSchema.",
        "3. Execute: `call_tool(server, tool_name, arguments)`.",
        "",
        "## Available servers",
        "",
    ]

    catalog = None
    if catalog_path:
        try:
            with open(catalog_path) as f:
                catalog = json.load(f)
        except (OSError, json.JSONDecodeError):
            catalog = None

    if catalog is None:
        lines.append("(catalog.json not available — run generate.sh to build it)")
    else:
        lines.append("| Server | Tools |")
        lines.append("| --- | --- |")
        for backend in catalog.get("backends", []):
            server_id = backend.get("id", "?")
            tool_count = backend.get("tool_count", len(backend.get("tools", [])))
            lines.append(f"| {server_id} | {tool_count} |")

    return "\n".join(lines)


def main() -> None:
    """Read the hook's stdin payload, then emit additionalContext as JSON."""
    hook_event_name = "UserPromptSubmit"
    try:
        payload = json.load(sys.stdin)
        hook_event_name = payload.get("hook_event_name", hook_event_name)
    except (json.JSONDecodeError, ValueError):
        pass

    catalog_path = sys.argv[1] if len(sys.argv) > 1 else None
    output = {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": build_context(catalog_path),
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
