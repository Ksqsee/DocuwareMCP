# DocuWare for Claude

A read-only [MCP](https://modelcontextprotocol.io) server that lets Claude search and read
documents in DocuWare. It runs as a Cloudflare Worker and is added to Claude as a custom
connector. Colleagues sign in with their normal DocuWare username and password, and see
exactly what DocuWare already allows them to see. Nothing can be changed or deleted.

Tools: `status`, `list_archives`, `describe_archive`, `search`, `get_document`,
`get_document_text` (OCR text, capped by `max_chars`).

## Going live (once DocuWare is set up)

Requirements: DocuWare Cloud (or DocuWare 7.10+ reachable from the internet), and users who
sign in with a DocuWare username + password (not Microsoft-SSO-only).

1. In `wrangler.jsonc`, set `DW_URL` to your DocuWare address, e.g. `https://yourcompany.docuware.cloud`.
2. Deploy: `npm install && npx wrangler deploy`.
3. In Claude (organization settings → Connectors), add a custom connector with the URL
   `https://docuware-mcp.ksqsebastian.workers.dev/mcp`.
4. Colleagues click **Connect** and sign in with their DocuWare login.

Someone leaves: disable them in DocuWare. Their Claude access ends within the hour.

## Security

- Only Claude's callback URLs can register as a client, so the login page can't be used to
  phish passwords for another app.
- The login form is bound to the browser that opened it (no CSRF), can't be framed, escapes
  all input, and is rate-limited per IP and per username; a username is blocked for 15
  minutes after 5 wrong passwords, so attackers can't lock colleagues out of DocuWare.
- Passwords are stored only inside each sign-in, encrypted with a key that only that sign-in's
  token can unwrap ([workers-oauth-provider](https://github.com/cloudflare/workers-oauth-provider)).
  When DocuWare rejects a stored password, it is never retried.
- The password is only ever sent to the DocuWare host or `*.docuware.cloud`, over https,
  without following redirects. The DocuWare token never leaves the DocuWare host.
- Document ids must be numeric; filter values are escaped; error bodies are not logged.

`test/sec-review.test.ts` contains the adversarial security tests; `test/worker.test.ts`
covers the sign-in flow and tools. Both run inside workerd against a fake DocuWare:

```
npm install
npm run check   # type-check
npm test
```

## Credits

The DocuWare API calls and the filter design are ported from
[sniner/docuware-client](https://github.com/sniner/docuware-client) and
[sniner/docuware-mcp](https://github.com/sniner/docuware-mcp) by Stefan Schönberger
(BSD-3-Clause, see `LICENSE`).
