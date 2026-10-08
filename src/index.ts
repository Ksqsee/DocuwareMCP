import OAuthProvider, { type AuthRequest, type OAuthHelpers, OAuthError } from "@cloudflare/workers-oauth-provider";
import { createMcpHandler } from "agents/mcp/server";
import { env as globalEnv } from "cloudflare:workers";
import { DocuWareError, login } from "./docuware";
import { buildServer, type Props, sha256 } from "./tools";

export interface Env {
  OAUTH_KV: KVNamespace;
  OAUTH_PROVIDER: OAuthHelpers;
  DW_URL: string;
  PUBLIC_URL: string;
  LOGIN_LIMIT: RateLimit;
  USER_LIMIT: RateLimit;
  REGISTER_LIMIT: RateLimit;
}

const DAY = 86_400;
const MAX_SIGNIN_AGE_MS = 30 * DAY * 1000;
const FAILS_BEFORE_BLOCK = 5;
const BLOCK_SECONDS = 15 * 60;

// Only Claude may receive sign-ins, so nobody can register a look-alike app
// and use this login page to phish a colleague's DocuWare password.
export const ALLOWED_REDIRECTS = new Set([
  "https://claude.ai/api/mcp/auth_callback",
  "https://claude.com/api/mcp/auth_callback",
]);

const base = (env: Env) => env.DW_URL.replace(/\/+$/, "");

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function page(handle: string, redirectHost: string, error = "", username = ""): string {
  return `<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DocuWare für Claude – Anmelden</title>
<style>
body{font-family:system-ui,sans-serif;background:#f4f4f5;margin:0;display:grid;place-items:center;min-height:100vh}
form{background:#fff;padding:2rem;border-radius:12px;max-width:22rem;width:calc(100% - 2rem);box-shadow:0 1px 4px #0002}
h1{font-size:1.25rem;margin:0 0 .5rem}p{color:#52525b;margin:0 0 1rem;font-size:.9rem}
label{display:block;margin-bottom:.35rem;font-weight:600}
input{width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem;margin-bottom:1rem;border:1px solid #a1a1aa;border-radius:6px}
button{width:100%;padding:.7rem;font-size:1rem;background:#1d4ed8;color:#fff;border:0;border-radius:8px;cursor:pointer}
.err{color:#b91c1c;font-weight:600}
</style></head><body>
<form method="post">
<h1>DocuWare für Claude</h1>
<p>Melden Sie sich mit Ihrem normalen DocuWare-Benutzer an. Claude kann danach Ihre Dokumente lesen, aber nichts ändern oder löschen.</p>
${error ? `<p class="err" role="alert">${esc(error)}</p>` : ""}
<input type="hidden" name="handle" value="${esc(handle)}">
<label for="u">DocuWare-Benutzername</label>
<input id="u" name="username" autocomplete="username" required autofocus value="${esc(username)}">
<label for="p">Passwort</label>
<input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Anmelden</button>
<p style="margin:1rem 0 0;font-size:.8rem">Zugriff geht an: ${esc(redirectHost)}</p>
<p style="margin:.5rem 0 0;font-size:.8rem"><strong>Nur anmelden, wenn Sie gerade selbst in Claude bei „DocuWare“ auf „Verbinden“ geklickt haben.</strong> Hat Ihnen jemand diesen Link geschickt, brechen Sie ab.</p>
</form></body></html>`;
}

const html = (body: string, headers: Headers, status = 200) => {
  headers.set("Content-Type", "text/html; charset=utf-8");
  return new Response(body, { status, headers });
};

async function authorize(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  if (request.method === "GET") {
    let auth: AuthRequest;
    try {
      auth = await oauth.parseAuthRequest(request);
    } catch {
      return new Response("Ungültige Anmeldeanfrage.", { status: 400 });
    }
    if (!ALLOWED_REDIRECTS.has(auth.redirectUri)) return new Response("Nicht erlaubt.", { status: 400 });
    const consent = await oauth.beginConsent(auth);
    return html(page(consent.handle, new URL(auth.redirectUri).host), consent.headers);
  }

  // Each try costs a DocuWare login, and DocuWare locks accounts after repeated failures:
  // limit per IP, per username, and block a username for 15 minutes after 5 failures.
  const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
  const tooMany = () => new Response("Zu viele Anmeldeversuche. Bitte später erneut versuchen.", { status: 429 });
  if (!(await env.LOGIN_LIMIT.limit({ key: ip })).success) return tooMany();
  // Only a browser that opened the login page carries the consent cookie; without it,
  // never touch DocuWare (otherwise this form is an open password checker).
  if (!(request.headers.get("Cookie") ?? "").includes("__Host-oauth-"))
    return new Response("Die Anmeldung ist abgelaufen. Bitte in Claude erneut auf Verbinden klicken.", { status: 400 });

  const form = await request.formData();
  const handle = String(form.get("handle") ?? "");
  const username = String(form.get("username") ?? "").trim();
  const password = String(form.get("password") ?? "");
  // Storage keys use a hash, so nobody with access to the Cloudflare storage can read usernames.
  const userHash = await sha256(username.toLowerCase());
  const failKey = `login-fails:${userHash}`;
  if (!(await env.USER_LIMIT.limit({ key: username.toLowerCase() })).success) return tooMany();
  const fails = Number(await env.OAUTH_KV.get(failKey)) || 0;
  if (fails >= FAILS_BEFORE_BLOCK) return tooMany();
  try {
    // Check the password against DocuWare before using up the one-time consent handle.
    const { token } = await login(base(env), { username, password });
    const approved = await oauth.approveConsent(request, handle);
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId: userHash,
      metadata: {},
      scope: approved.request.scope,
      props: { username, password, dwToken: token, since: Date.now() } satisfies Props,
    });
    if (fails) await env.OAUTH_KV.delete(failKey);
    console.log(`login ok: ${username}`);
    approved.headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers: approved.headers });
  } catch (e) {
    if (!(e instanceof DocuWareError)) {
      console.error(e);
      return new Response("Die Anmeldung ist abgelaufen. Bitte in Claude erneut auf Verbinden klicken.", { status: 400 });
    }
    if (e.badCredentials) await env.OAUTH_KV.put(failKey, String(fails + 1), { expirationTtl: BLOCK_SECONDS });
    await new Promise((r) => setTimeout(r, 1000)); // slows password guessing
    const headers = new Headers({ "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'", "Cache-Control": "no-store" });
    return html(page(handle, "claude.ai", e.message, username), headers, 401);
  }
}

const mcp = {
  fetch(request: Request, env: Env, ctx: ExecutionContext & { props: Props }) {
    return createMcpHandler(() => buildServer(base(env), ctx.props, env.OAUTH_KV))(request, env, ctx);
  },
};

const provider = new OAuthProvider<Env>({
  apiRoute: "/mcp",
  apiHandler: mcp as any,
  defaultHandler: {
    fetch(request: Request, env: Env) {
      if (new URL(request.url).pathname === "/authorize") return authorize(request, env);
      return new Response("DocuWare MCP. In Claude als Connector hinzufügen: <diese Adresse>/mcp", { status: 404 });
    },
  } as any,
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register",
  refreshTokenIdleTTL: 7 * DAY,
  resourceMetadata: { resource: `${(globalEnv as unknown as Env).PUBLIC_URL.replace(/\/+$/, "")}/mcp` },
  clientRegistrationCallback: ({ clientMetadata }) => {
    const uris = (clientMetadata.redirect_uris as unknown[]) ?? [];
    if (!uris.length || !uris.every((u) => typeof u === "string" && ALLOWED_REDIRECTS.has(u)))
      return { code: "invalid_redirect_uri", description: "Only Claude may connect to this server." };
  },
  // Every refresh signs in to DocuWare again: a fresh DocuWare token, and a user who was
  // disabled or changed their password loses access within the hour.
  tokenExchangeCallback: async ({ grantType, props, env }) => {
    if (grantType !== "refresh_token") return;
    const p = props as Props;
    // A sign-in ends 7 days after its last use (refreshTokenIdleTTL) and 30 days after login at the latest.
    if (!p.since || Date.now() - p.since > MAX_SIGNIN_AGE_MS)
      throw new OAuthError("invalid_grant", { description: "Sign-in older than 30 days" });
    let fresh;
    try {
      fresh = await login(base(env as Env), p);
    } catch (e) {
      // Wrong password now: end this sign-in for good. DocuWare down: just fail this refresh.
      const gone = e instanceof DocuWareError && e.status === 401;
      throw new OAuthError(gone ? "invalid_grant" : "temporarily_unavailable", { description: "DocuWare sign-in failed" });
    }
    const { token, expiresIn } = fresh;
    return { accessTokenProps: { ...p, dwToken: token }, newProps: { ...p, dwToken: undefined }, accessTokenTTL: Math.min(expiresIn, 3600) };
  },
});

// Anonymous endpoints that write to KV (client registration, opening the login page)
// get a per-IP limit, so nobody can fill storage or burn the daily KV write quota.
export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const { pathname } = new URL(request.url);
    const writes = (pathname === "/register" && request.method === "POST") || (pathname === "/authorize" && request.method === "GET");
    if (writes && !(await env.REGISTER_LIMIT.limit({ key: request.headers.get("CF-Connecting-IP") ?? "unknown" })).success)
      return new Response("Too many requests", { status: 429 });
    return provider.fetch(request, env, ctx);
  },
};
