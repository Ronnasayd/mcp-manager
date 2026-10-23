# ADR-001 – call_tool Returns the Backend CallToolResult Unchanged

> **Date:** 2026-10-23
> **Status:** Accepted
> **Deciders:** Project owner

## Context

mcp-manager proxies tool calls to backend MCP servers. `call_tool` kept only the backend's
`content` list and wrapped it in a custom envelope (`success`, `result`, `error`, `elapsed_ms`).
The MCP framework then serialized that envelope as a text block. The effect: backend JSON
reached agents double-encoded, image blocks never arrived as images, and a backend reporting
`isError: true` was shown as `success: true`. The protocol's own `structuredContent` and `_meta`
fields were dropped.

## Decision

The proxy is transparent. `call_tool` returns the backend's `CallToolResult` as the backend sent
it: content blocks, `isError`, `structuredContent` and `_meta`. Failures raised by the proxy
itself (unknown tool, invalid arguments, timeout, backend crash) use the same shape: `isError: true`
plus a text block. The proxy never interprets the text inside blocks, even when it is JSON.
Timing goes to logs, not to the response.

## Considered Alternatives

| Alternative                                                                              | Pros                                                                            | Cons                                                                                              |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Faithful passthrough (chosen)                                                            | Agents see exactly what the backend produced; images render; one error contract | Breaks callers of the old envelope; latency no longer in the response                             |
| Keep the envelope, unwrap the inner result (parse JSON text, prefer `structuredContent`) | Small contract change                                                           | Still serialized as text, so images never render; parsing text is a guess that can change meaning |
| Hybrid: native content blocks, envelope in `structuredContent`                           | Keeps images and proxy metadata                                                 | More code; clients that read only `content` lose the metadata; two places to look for status      |

## Consequences

- Breaking change for any consumer reading `success` / `result` / `error` / `elapsed_ms`.
- Backends that put JSON inside text blocks still deliver a string the agent must parse. That is
  the backend's choice, and the proxy deliberately does not "fix" it.
- Off-spec backend results are not rejected. They are passed through as raw JSON text with a
  logged warning, so data stays visible.
- Do not reintroduce an envelope or JSON-in-text parsing without superseding this ADR.

## Related ADRs

None.
