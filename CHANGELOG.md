# Changelog

## 0.2.0 (2026-08-31)

### Changed

- **The server now speaks the sessionless MCP revision 2026-07-28** alongside the older
  handshake-based protocol; each client is answered in whichever it asks for. Under the hood the
  FastMCP framework made way for the official MCP Python SDK v2 — tool names, arguments and
  results are unchanged
- **HTTP mode answers every request statelessly**, so instances behind a load balancer need no
  session affinity
- **The default HTTP mount path is `/mcp`** instead of `/mcp/`. A deployment that pinned the old
  URL keeps it with `--path /mcp/` or `DW_MCP_PATH=/mcp/`

## 0.1.5 (2026-05-18)

### Fixed

- **`search` accepts `filters` and `order_by` sent as JSON-encoded strings.** Some clients emit
  nested tool arguments that way; such calls now succeed instead of failing validation.
  Structured objects remain the documented form

## 0.1.4 (2026-05-18)

### Changed

- **The `search` tool description spells out the distinct roles of `filters` and `order_by`** —
  filters select, sorting ranks — so models stop using a sort with a small limit as a substitute
  for a bound

## 0.1.3 (2026-05-14)

### Added

- **OAuth2 client-credentials authentication** via `DW_CLIENT_ID` / `DW_CLIENT_SECRET`, for
  service-to-service deployments without a user account. Requires a DocuWare "Trusted / Service"
  App Registration (DocuWare 7.10 or later)

## 0.1.2 (2026-05-12)

### Added

- **`order_by` on `search`**: server-side ranking by one or more fields, ascending or descending

## 0.1.1 (2026-05-11)

### Added

- **`get_document_text`** returns the OCR fulltext of a document, one text block per attachment;
  an attachment without OCR reports an error in place of its text instead of failing the whole
  call
- **`--http` serves Streamable HTTP** for network clients such as Open WebUI; stdio remains the
  default

### Fixed

- **Ctrl-C shuts the server down cleanly** instead of dumping a traceback

## 0.1.0 (2026-05-08)

Initial release: an MCP server exposing a DocuWare DMS through a database-style, read-only API —
`list_archives`, `describe_archive`, `search` with a structured filter DSL, `get_document`, and
`status` — speaking MCP over stdio.
