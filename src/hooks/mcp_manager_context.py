#!/usr/bin/env python3
"""SessionStart/UserPromptSubmit hook: injects mcp-manager usage context.

Usage: mcp_manager_context.py <path-to-catalog.json>
"""

import json
import sys

SERVER_DESCRIPTIONS: dict[str, str] = {
    "rea": "Reverse engineering: native, managed, Electron, firmware, browser targets",
    "rag-rat": "Repo intel: semantic search, symbols, callers, impact",
    "codebase-memory": "Code graph: index repo, trace calls, file outlines",
    "serena": "LSP-based semantic code navigation and symbol-level editing",
    "godot": "Godot engine: launch editor, run projects, read debug output",
    "magick": "ImageMagick: convert, resize, and transform images",
    "image-tools": "Image inspection: load, dimensions, crop, sample pixel colors",
    "blender": "Blender: inspect scenes and execute Python code in Blender",
    "mobile-mcp": "Android/iOS device automation: apps, taps, screenshots",
    "notebooklm": "NotebookLM: manage notebooks, ask questions on sources",
    "iconify": "Iconify: search and fetch icons/icon sets (SVG)",
    "sequentialthinking": "Structured step-by-step reasoning for complex problems",
    "taskmaster-ai": "Task Master: project tasks, subtasks, statuses, PRDs",
    "chrome-devtools": "Chrome DevTools: drive browser, network, console, perf",
    "exa": "Exa: web search and web page fetch",
    "next-devtools": "Next.js dev tools: docs, running-app inspection, browser eval",
    "a11y": "Accessibility audits of web pages (axe)",
    "playwright": "Browser automation and E2E tests (Playwright)",
    "csv-editor": "CSV load/edit/analyze/export",
    "html-to-markdown": "Convert HTML/web pages to Markdown and save it",
    "json-mcp-filter": "Inspect JSON schema and filter large JSON files",
    "large-file": "Read, search, and navigate large files in chunks",
    "laya": "Laya: fast(cpu) typed-decision classifier (choice/score/prob)",
    "clef": "Clef: Web-api LLM typed-decision classifier (choice/score/probability)",
    "drawio": "draw.io: generate/edit diagrams from XML, CSV, Mermaid; search shapes",
}


def load_json(path: str | None) -> dict | None:
    """Load a JSON file, returning None when missing or invalid."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


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
        "It exposes 4 tools: `list_servers`, `search_tools`, `get_tool_schema`, "
        "`call_tool`.",
        "",
        "Flow to use any backend tool:",
        "1. Find the tool: `search_tools` (fuzzy match by keyword) or "
        "`list_servers` to browse by backend.",
        "2. Load its schema: `get_tool_schema(server, tool_name)` — ALWAYS do this "
        "before calling, arguments must match the returned inputSchema.",
        "3. Execute: `call_tool(server, tool_name, arguments)`.",
        "",
        "## Available servers",
        "",
    ]

    catalog = load_json(catalog_path)

    if catalog is None:
        lines.append("(catalog.json not available — run generate.sh to build it)")
    else:
        lines.append("| Server | Tools | Description |")
        lines.append("| --- | --- | --- |")
        for backend in catalog.get("backends", []):
            server_id = backend.get("id", "?")
            tool_count = backend.get("tool_count", len(backend.get("tools", [])))
            description = SERVER_DESCRIPTIONS.get(server_id, "")
            lines.append(f"| {server_id} | {tool_count} | {description} |")

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
    sys.stdout.write(json.dumps(output) + "\n")


if __name__ == "__main__":
    main()
