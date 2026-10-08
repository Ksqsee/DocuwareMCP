# docuware-mcp

> **Vendored copy.** This repository contains
> [sniner/docuware-mcp](https://github.com/sniner/docuware-mcp) at commit
> `cd567d3` (v0.2.0, 2026-08-31), BSD-3-Clause, by the author of
> [docuware-client](https://github.com/sniner/docuware-client). Upstream is
> **read-only by design** — it exposes no tools that create, edit or delete
> documents. Tools: `status`, `list_archives`, `describe_archive`, `search`,
> `get_document`, `get_document_text`.
>
> To run this copy instead of the PyPI package, point your MCP client at the
> checkout: `uv run --directory /path/to/DocuwareMCP docuware-mcp`
> (see `.mcp.json.example`). Alternatively use the published package with
> `uvx docuware-mcp`.

A Model Context Protocol (MCP) server that exposes a DocuWare DMS to
LLM-based agents through a database-style API. It serves both the
current sessionless MCP revision (2026-07-28) and the older
handshake-based protocol — each client gets whichever it asks for.

This is an independent project with no affiliation to DocuWare GmbH.

## Configuration

Credentials are read from the `docuware-client` standard environment
variables:

```
DW_URL=https://dms.example.com
DW_USERNAME=service_account
DW_PASSWORD=<secret>
DW_ORG=<org>
```

Alternatively, point `DW_CREDENTIALS_FILE` at a JSON file — useful for
switching between test and production systems, or for keeping secrets
out of shell history:

```
DW_CREDENTIALS_FILE=/path/to/credentials.json
```

The file uses the same keys as the environment variables:

```json
{
    "url": "https://dms.example.com",
    "username": "service_account",
    "password": "<secret>",
    "organization": "Acme GmbH"
}
```

`organization` is optional if the service account belongs to a single
organization. Make sure the file is not world-readable (`chmod 600`).

For service-to-service deployments, OAuth2 Client Credentials is the
recommended flow — no user account, no browser, no rotating refresh
tokens. Requires a DocuWare "Trusted / Service" App Registration that
provides a `client_secret`:

```
DW_URL=https://dms.example.com
DW_CLIENT_ID=<app-registration-id>
DW_CLIENT_SECRET=<secret>
```

This path is preferred over `DW_USERNAME` / `DW_PASSWORD` for unattended
deployments (containers, systemd units, Claude Desktop on a workstation
with secrets injected from a manager). `DW_CREDENTIALS_FILE` takes
precedence if both are set.

For internal DocuWare installations with self-signed or private-CA
certificates, TLS verification can be disabled with
`DW_VERIFY_CERT=false`. **Do not use this against production systems** —
it disables protection against man-in-the-middle attacks.

OAuth2 requires DocuWare 7.10 or later.

## Use with an MCP client

By default `docuware-mcp` speaks MCP over stdio: an MCP client (Claude
Desktop, Claude Code, …) launches it as a subprocess and talks to it
over stdin/stdout. You don't run it yourself — the client does. For
clients that connect over the network instead, see
[Serving over HTTP](#serving-over-http) below.

The recommended install path is via [`uv`](https://docs.astral.sh/uv/),
because `uvx` will fetch and run the package on demand without a global
install. Install `uv` once (`brew install uv` on macOS,
`curl -LsSf https://astral.sh/uv/install.sh | sh` on Linux,
`irm https://astral.sh/uv/install.ps1 | iex` in PowerShell on Windows),
then add this entry to your client's MCP config:

```json
{
  "mcpServers": {
    "docuware": {
      "command": "uvx",
      "args": ["docuware-mcp"],
      "env": {
        "DW_CREDENTIALS_FILE": "/path/to/credentials.json"
      }
    }
  }
}
```

The config file lives at:

- **Claude Desktop**: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows)
- **Claude Code**: `.mcp.json` in your project root (or run `claude mcp add docuware -- uvx docuware-mcp`)

For [OpenCode](https://opencode.ai/) the shape is slightly different —
add to `opencode.json` (project) or `~/.config/opencode/opencode.json`
(user):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "docuware": {
      "type": "local",
      "command": ["uvx", "docuware-mcp"],
      "environment": {
        "DW_CREDENTIALS_FILE": "/path/to/credentials.json"
      },
      "enabled": true
    }
  }
}
```

Restart the client after editing. The `docuware` server should then
appear in the available-tools list, exposing `list_archives`,
`describe_archive`, `search`, `get_document`, `get_document_text`,
and `status`.

## Serving over HTTP

Clients that cannot launch a subprocess — Open WebUI, for example —
connect over the network instead. For those, start the server yourself
with `--http`:

```
docuware-mcp --http
```

It then accepts Streamable HTTP connections on
`http://127.0.0.1:8765/mcp`. Bind address, port, and path can be
changed with `--host`, `--port`, and `--path`, or the environment
variables `DW_MCP_HOST`, `DW_MCP_PORT`, and `DW_MCP_PATH`.

The HTTP endpoint performs no authentication of its own: anyone who can
reach it can query the DocuWare account it is configured with. Keep it
on localhost, or put an authenticating reverse proxy in front before
exposing it beyond the machine.

## Running directly (for debugging)

If you've cloned this repo and want to poke at the server with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector)
or call it from a script:

```
docuware-mcp
```

Speaks MCP over stdio — same protocol the clients above use.

## License

BSD-3-Clause.
