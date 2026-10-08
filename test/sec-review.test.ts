// Every test asserts the SAFE behaviour; tests named "fixed: …" cover weaknesses the review found and the code now prevents.
import { env, exports } from "cloudflare:workers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DW, type FakeState, fakeDocuWare, GOOD, newState } from "./fake-docuware";
import { escape } from "../src/filters";

const ORIGIN = "https://docuware-mcp.test";
const CALLBACK = "https://claude.ai/api/mcp/auth_callback";
const RESOURCE = `${ORIGIN}/mcp`;
let dw: FakeState;
// Optional per-test overrides on top of the fake DocuWare.
let override: ((req: Request) => Promise<Response | undefined> | Response | undefined) | undefined;

beforeEach(() => {
  dw = newState();
  // A fresh username per test: the per-username login limit would otherwise carry over.
  GOOD.username = `anna+${crypto.randomUUID()}`;
  override = undefined;
  const base = fakeDocuWare(dw);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (override) {
      const req = new Request(input, init);
      const r = await override(req.clone());
      if (r) {
        dw.calls.push({ method: req.method, url: req.url, auth: req.headers.get("Authorization"), body: req.method === "POST" ? await req.text() : undefined });
        return r;
      }
    }
    return base(input, init);
  });
});
afterEach(() => vi.restoreAllMocks());

// Cloudflare always sets CF-Connecting-IP; give each request its own unless a test picks one.
const call = (path: string, init?: RequestInit): Promise<Response> => {
  const req = new Request(`${ORIGIN}${path}`, init);
  if (!req.headers.has("CF-Connecting-IP")) req.headers.set("CF-Connecting-IP", crypto.randomUUID());
  return (exports as any).default.fetch(req);
};

async function register(redirect: unknown = CALLBACK, extra: object = {}) {
  return call("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ redirect_uris: [redirect], token_endpoint_auth_method: "none", client_name: "Claude", ...extra }),
  });
}

async function pkce(verifier = "v".repeat(64)) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return { verifier, challenge };
}

async function authorizeUrl(client_id: string, over: Record<string, string | null> = {}) {
  const { challenge } = await pkce();
  const p: Record<string, string | null> = {
    response_type: "code",
    client_id,
    redirect_uri: CALLBACK,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state: "xyz",
    resource: RESOURCE,
    ...over,
  };
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) if (v !== null) qs.set(k, v);
  return `/authorize?${qs}`;
}

async function openLogin(extraReg: object = {}) {
  const { client_id } = (await (await register(CALLBACK, extraReg)).json()) as { client_id: string };
  const { verifier } = await pkce();
  const page = await call(await authorizeUrl(client_id));
  expect(page.status).toBe(200);
  const html = await page.text();
  const handle = /name="handle" value="([^"]+)"/.exec(html)![1];
  const cookie = page.headers.getSetCookie().map((c: string) => c.split(";")[0]).join("; ");
  return { client_id, verifier, handle, cookie, html, headers: page.headers };
}

function submit(login: { handle: string; cookie: string }, username: string, password: string, ip: string | null = crypto.randomUUID()) {
  const headers: Record<string, string> = { Cookie: login.cookie, "Content-Type": "application/x-www-form-urlencoded" };
  if (ip !== null) headers["CF-Connecting-IP"] = ip;
  return call("/authorize", { method: "POST", headers, body: new URLSearchParams({ handle: login.handle, username, password }), redirect: "manual" });
}

const tokenReq = (params: Record<string, string>) =>
  call("/token", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(params) });

async function codeFor() {
  const login = await openLogin();
  const res = await submit(login, GOOD.username, GOOD.password);
  expect(res.status).toBe(302);
  return { ...login, code: new URL(res.headers.get("Location")!).searchParams.get("code")! };
}

async function signIn() {
  const l = await codeFor();
  const token = await tokenReq({
    grant_type: "authorization_code",
    code: l.code,
    redirect_uri: CALLBACK,
    client_id: l.client_id,
    code_verifier: l.verifier,
    resource: RESOURCE,
  });
  expect(token.status).toBe(200);
  return { ...((await token.json()) as { access_token: string; refresh_token: string }), client_id: l.client_id };
}

async function mcp(token: string | null, method: string, params: object = {}, path = "/mcp") {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await call(path, { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  if (res.status !== 200) return { status: res.status, body: null as any };
  const text = await res.text();
  const json = text.startsWith("{") ? JSON.parse(text) : JSON.parse(/^data: (.*)$/m.exec(text)![1]);
  return { status: 200, body: json };
}

async function tool(token: string, name: string, args: object) {
  const { body } = await mcp(token, "tools/call", { name, arguments: args });
  const text = body.result.content[0].text as string;
  return { isError: !!body.result.isError, text };
}

const tokenEndpointCalls = () => dw.calls.filter((c) => c.url.endsWith("/connect/token"));

describe("OAuth", () => {
  it("rejects redirect_uri look-alikes at registration", async () => {
    for (const u of [
      "https://claude.ai/api/mcp/auth_callback/",
      "https://CLAUDE.AI/api/mcp/auth_callback",
      "https://claude.ai/api/mcp/auth_callback?x=1",
      "https://claude.ai/api/mcp/auth_callback#x",
      "https://claude.ai:443/api/mcp/auth_callback",
      "https://claude.ai:8443/api/mcp/auth_callback",
      "https://claude.ai@evil.com/api/mcp/auth_callback",
      "https://evil.com@claude.ai/api/mcp/auth_callback",
      "http://claude.ai/api/mcp/auth_callback",
      "https://claude.ai/api/mcp/auth_callback/../../x",
      "https://claude.ai./api/mcp/auth_callback",
    ])
      expect((await register(u)).status, u).toBe(400);
    // Mixed list: one good, one evil.
    const mixed = await call("/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ redirect_uris: [CALLBACK, "https://evil.com/cb"], token_endpoint_auth_method: "none" }),
    });
    expect(mixed.status).toBe(400);
  });

  it("rejects redirect_uri variants at /authorize for a legit client", async () => {
    const { client_id } = (await (await register()).json()) as { client_id: string };
    for (const u of [`${CALLBACK}/`, `${CALLBACK}?a=1`, "https://claude.ai@evil.com/api/mcp/auth_callback", "https://evil.com/cb", "https://claude.com/api/mcp/auth_callback"]) {
      const r = await call(await authorizeUrl(client_id, { redirect_uri: u }), { redirect: "manual" });
      expect(r.status, u).not.toBe(200);
      const loc = r.headers.get("Location") ?? "";
      expect(loc.includes("evil.com"), u).toBe(false);
    }
  });

  it("requires S256 PKCE (plain and missing are rejected)", async () => {
    const { client_id } = (await (await register()).json()) as { client_id: string };
    const plain = await call(await authorizeUrl(client_id, { code_challenge: "v".repeat(64), code_challenge_method: "plain" }), { redirect: "manual" });
    expect(plain.status).not.toBe(200);
    const none = await call(await authorizeUrl(client_id, { code_challenge: null, code_challenge_method: null }), { redirect: "manual" });
    expect(none.status).not.toBe(200);
  });

  it("a wrong PKCE verifier, another client, or another redirect_uri cannot redeem a code", async () => {
    const l = await codeFor();
    const other = ((await (await register()).json()) as { client_id: string }).client_id;
    const base = { grant_type: "authorization_code", code: l.code, redirect_uri: CALLBACK, client_id: l.client_id, code_verifier: l.verifier, resource: RESOURCE };
    expect((await tokenReq({ ...base, client_id: other })).status).toBe(400);
    expect((await tokenReq({ ...base, code_verifier: "w".repeat(64) })).status).toBe(400);
    expect((await tokenReq({ ...base, redirect_uri: "https://claude.com/api/mcp/auth_callback" })).status).toBe(400);
  });

  it("refresh tokens are bound to the client and rotate", async () => {
    const { refresh_token, client_id } = await signIn();
    const other = ((await (await register()).json()) as { client_id: string }).client_id;
    expect((await tokenReq({ grant_type: "refresh_token", refresh_token, client_id: other })).status).toBe(400);
    const r1 = await tokenReq({ grant_type: "refresh_token", refresh_token, client_id });
    expect(r1.status).toBe(200);
    const rt2 = ((await r1.json()) as any).refresh_token;
    const r2 = await tokenReq({ grant_type: "refresh_token", refresh_token: rt2, client_id });
    expect(r2.status).toBe(200);
    // rt1 is now two generations old.
    expect((await tokenReq({ grant_type: "refresh_token", refresh_token, client_id })).status).toBe(400);
  });

  it("token for another resource is refused; token in query string is ignored; token-exchange is off", async () => {
    const l = await codeFor();
    const bad = await tokenReq({ grant_type: "authorization_code", code: l.code, redirect_uri: CALLBACK, client_id: l.client_id, code_verifier: l.verifier, resource: "https://evil.example/mcp" });
    expect(bad.status).toBe(400);
    const { access_token, client_id } = await signIn();
    const q = await call(`/mcp?access_token=${encodeURIComponent(access_token)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
    });
    expect(q.status).toBe(401);
    const x = await tokenReq({
      grant_type: "urn:ietf:params:oauth:grant-type:token-exchange",
      subject_token: access_token,
      subject_token_type: "urn:ietf:params:oauth:token-type:access_token",
      client_id,
    });
    expect(x.status).toBe(400);
  });

  it("CIMD (URL client_id) is not resolved", async () => {
    const r = await call(await authorizeUrl("https://evil.example/client.json"), { redirect: "manual" });
    expect(r.status).not.toBe(200);
    expect(dw.calls.some((c) => c.url.startsWith("https://evil.example"))).toBe(false);
  });

  it("a password change ends the grant: refresh fails once and the old access token dies", async () => {
    const { access_token, refresh_token, client_id } = await signIn();
    dw.password = "neu";
    const r = await tokenReq({ grant_type: "refresh_token", refresh_token, client_id });
    expect(r.status).toBe(400);
    expect((await mcp(access_token, "tools/list")).status).toBe(401);
    const before = tokenEndpointCalls().length;
    await tokenReq({ grant_type: "refresh_token", refresh_token, client_id });
    expect(tokenEndpointCalls().length).toBe(before); // no further DocuWare login attempts
  });
});

describe("login page", () => {
  it("GET and error POST both carry anti-framing + no-store; client_name is never rendered", async () => {
    const login = await openLogin({ client_name: "<img src=x onerror=alert(1)>" });
    expect(login.headers.get("X-Frame-Options")).toBe("DENY");
    expect(login.headers.get("Content-Security-Policy")).toContain("frame-ancestors 'none'");
    expect(login.headers.get("Cache-Control")).toContain("no-store");
    expect(login.html).not.toContain("onerror");
    const res = await submit(login, "x", "y");
    expect(res.status).toBe(401);
    expect(res.headers.get("X-Frame-Options")).toBe("DENY");
    expect(res.headers.get("Cache-Control")).toContain("no-store");
  });

  it("DocuWare-provided status text cannot inject HTML into the error page", async () => {
    override = (req) => (new URL(req.url).pathname.endsWith("/connect/token") ? new Response("<script>x</script>", { status: 502 }) : undefined);
    const login = await openLogin();
    const html = await (await submit(login, '<svg onload=1>', "p")).text();
    expect(html).not.toContain("<script>x");
    expect(html).not.toContain("<svg");
  });

  it("the consent handle is single-use after a successful sign-in", async () => {
    const login = await openLogin();
    expect((await submit(login, GOOD.username, GOOD.password)).status).toBe(302);
    const again = await submit(login, GOOD.username, GOOD.password);
    expect(again.status).toBe(400);
    expect(again.headers.get("Location")).toBeNull();
  });

  it("fixed: POST /authorize without any consent handle is still a DocuWare password oracle", async () => {
    // Safe behaviour: no DocuWare login attempt unless the handle+cookie are valid.
    const res = await call("/authorize", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "CF-Connecting-IP": crypto.randomUUID() },
      body: new URLSearchParams({ handle: "nope", username: GOOD.username, password: GOOD.password }),
    });
    expect(tokenEndpointCalls().length).toBe(0);
    expect(res.status).toBe(400);
  });

  it("fixed: rate limit is only per IP, so one account can be guessed/locked from many IPs", { timeout: 30_000 }, async () => {
    const login = await openLogin();
    const statuses = await Promise.all(Array.from({ length: 25 }, (_, i) => submit(login, GOOD.username, `guess${i}`, `198.51.100.${i}`).then((r) => r.status)));
    // Safe behaviour: a per-username budget stops this.
    expect(statuses).toContain(429);
  });

  it("credentials never reach logs or plaintext KV", async () => {
    const logs: string[] = [];
    for (const m of ["log", "error", "warn", "info", "debug"] as const)
      vi.spyOn(console, m).mockImplementation((...a: unknown[]) => void logs.push(a.map((x) => (x instanceof Error ? `${x.message} ${x.stack}` : typeof x === "string" ? x : JSON.stringify(x))).join(" ")));
    const { access_token, refresh_token, client_id } = await signIn();
    // Exercise error paths too: a dead handle after a good password, a 500 from DocuWare inside a tool.
    const login = await openLogin();
    await submit({ ...login, handle: "x" }, GOOD.username, GOOD.password);
    await tokenReq({ grant_type: "refresh_token", refresh_token, client_id });
    override = (req) => (new URL(req.url).pathname.includes("/Documents/") ? new Response("boom", { status: 500 }) : undefined);
    await tool(access_token, "get_document", { archive: "Archiv", document_id: "42" });
    expect(logs.join("\n")).not.toContain(GOOD.password);
    expect(logs.join("\n")).not.toContain("dw-token");
    const kv = (env as any).OAUTH_KV as KVNamespace;
    let cursor: string | undefined;
    do {
      const page = await kv.list({ cursor });
      for (const k of page.keys) {
        const v = (await kv.get(k.name)) ?? "";
        expect(v, k.name).not.toContain(GOOD.password);
        expect(v, k.name).not.toContain("dw-token");
        // Usernames are hashed: not in key names, values or key metadata.
        expect(`${k.name} ${v} ${JSON.stringify(k.metadata ?? null)}`, k.name).not.toContain(GOOD.username);
      }
      cursor = page.list_complete ? undefined : page.cursor;
    } while (cursor);
  });
});

describe("DocuWare client", () => {
  for (const href of ["//evil.test/x", "/\\evil.test/x", "\\\\evil.test\\x", "https://dw.test@evil.test/x", "https://dw.test.evil.test/x", "https:\\\\evil.test", "http://dw.test/x", "https://dw.test:8443/x"]) {
    it(`link ${JSON.stringify(href)} cannot carry the DocuWare token elsewhere`, async () => {
      const { access_token } = await signIn();
      override = (req) =>
        new URL(req.url).pathname.endsWith("/Documents/42")
          ? Response.json({ Id: 42, Sections: [{ Id: "s1", Links: [{ rel: "textshot", href }] }] })
          : undefined;
      await tool(access_token, "get_document_text", { archive: "Archiv", document_id: "42" });
      expect(dw.calls.filter((c) => !c.url.startsWith(`${DW}/`)).map((c) => c.url)).toEqual([]);
    });
  }

  it("fixed: login() POSTs the password to whatever token_endpoint discovery returns", async () => {
    override = (req) => {
      const p = new URL(req.url).pathname;
      if (p.endsWith("/.well-known/openid-configuration")) return Response.json({ token_endpoint: "https://evil.test/token" });
      if (req.url.startsWith("https://evil.test/")) return Response.json({ access_token: "dw-token", expires_in: 3600 });
      return undefined;
    };
    const login = await openLogin();
    await submit(login, GOOD.username, GOOD.password);
    expect(dw.calls.some((c) => c.url.startsWith("https://evil.test/") && (c.body ?? "").includes(GOOD.password))).toBe(false);
  });

  it("fixed: after a password change, every tool call retries the old password (account-lockout risk)", async () => {
    const { access_token } = await signIn();
    dw.password = "neu";
    // DocuWare now rejects the old DocuWare token as well.
    override = (req) => {
      const p = new URL(req.url).pathname;
      const platform = p === "/DocuWare/Platform" || (p.startsWith("/DocuWare/Platform/") && !p.startsWith("/DocuWare/Platform/Home/"));
      return platform ? new Response("unauthorized", { status: 401 }) : undefined;
    };
    const before = tokenEndpointCalls().length;
    for (let i = 0; i < 6; i++) await tool(access_token, "list_archives", {});
    // Safe behaviour: at most one failed password attempt, then the session is dead.
    expect(tokenEndpointCalls().length - before).toBeLessThanOrEqual(1);
  });

  it("fixed: a 403 (no permission) triggers a full password re-login on every call", async () => {
    const { access_token } = await signIn();
    override = (req) => (new URL(req.url).pathname.endsWith("/Documents/42") ? new Response("forbidden", { status: 403 }) : undefined);
    const before = tokenEndpointCalls().length;
    for (let i = 0; i < 3; i++) await tool(access_token, "get_document", { archive: "Archiv", document_id: "42" });
    expect(tokenEndpointCalls().length - before).toBe(0);
  });

  it("DocuWare error bodies reach the MCP client (300 chars max), never the password", async () => {
    const { access_token } = await signIn();
    override = (req) => (new URL(req.url).pathname.endsWith("/Documents/42") ? new Response("A".repeat(1000), { status: 500 }) : undefined);
    const r = await tool(access_token, "get_document", { archive: "Archiv", document_id: "42" });
    expect(r.isError).toBe(true);
    expect(r.text.length).toBeLessThan(340);
    expect(r.text).not.toContain(GOOD.password);
  });
});

describe("filters and arguments", () => {
  it("DocuWare operators cannot be smuggled into a value", () => {
    expect(escape("EMPTY()", true)).toBe("EMPTY\\(\\)");
    expect(escape("NOTEMPTY()", false)).toBe("NOTEMPTY\\(\\)");
    expect(escape("a*b?", true)).toBe("a\\*b\\?");
  });

  it("fixed: trailing / doubled backslashes are passed through unescaped", () => {
    // "\\(" in input yields "\\(" – if DocuWare reads "\\" as an escaped backslash, the "(" is live.
    expect(escape("x\\\\(", true)).not.toBe("x\\\\(");
  });

  it("fixed: order_by direction 'constructor'/'__proto__' is accepted (prototype lookup)", async () => {
    const { access_token } = await signIn();
    const r = await tool(access_token, "search", { archive: "Archiv", order_by: [{ field: "Datum", direction: "constructor" }] });
    expect(r.isError).toBe(true);
  });

  it("JSON-string filters with __proto__ keys do not pollute or crash", async () => {
    const { access_token } = await signIn();
    const r = await tool(access_token, "search", { archive: "Archiv", filters: '{"__proto__": {"polluted": 1}, "Belegart": "x"}' });
    expect(({} as any).polluted).toBeUndefined();
    expect(r.text).not.toContain("Internal error");
  });
});

describe("abuse", () => {
  it("fixed: anonymous DCR has no rate limit (unbounded KV writes)", async () => {
    const statuses = [];
    const register = () =>
      call("/register", {
        method: "POST",
        headers: { "Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.7" },
        body: JSON.stringify({ redirect_uris: [CALLBACK], token_endpoint_auth_method: "none" }),
      });
    for (let i = 0; i < 40; i++) statuses.push((await register()).status);
    expect(statuses).toContain(429);
  });

  it("CORS on /mcp reflects Origin but never allows credentials", async () => {
    const r = await call("/mcp", { method: "OPTIONS", headers: { Origin: "https://evil.example", "Access-Control-Request-Method": "POST" } });
    expect(r.headers.get("Access-Control-Allow-Credentials")).toBeNull();
  });
});

describe("sign-in lifetime and phishing warning", () => {
  it("login page warns not to sign in from a link someone sent", async () => {
    const { html } = await openLogin();
    expect(html).toContain("Hat Ihnen jemand diesen Link geschickt");
  });

  it("a sign-in older than 30 days cannot be refreshed, even while in use", async () => {
    const { refresh_token, client_id } = await signIn();
    vi.useFakeTimers({ toFake: ["Date"], now: Date.now() + 31 * 86_400_000 });
    try {
      const r = await tokenReq({ grant_type: "refresh_token", refresh_token, client_id });
      expect(r.status).toBe(400);
    } finally {
      vi.useRealTimers();
    }
  });
});
