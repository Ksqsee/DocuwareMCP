import { exports } from "cloudflare:workers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type FakeState, fakeDocuWare, GOOD, newState } from "./fake-docuware";

const ORIGIN = "https://docuware-mcp.test";
const CALLBACK = "https://claude.ai/api/mcp/auth_callback";
let dw: FakeState;

beforeEach(() => {
  dw = newState();
  vi.spyOn(globalThis, "fetch").mockImplementation(fakeDocuWare(dw));
});
afterEach(() => vi.restoreAllMocks());

const call = (path: string, init?: RequestInit) => (exports as any).default.fetch(new Request(`${ORIGIN}${path}`, init));

async function register(redirect = CALLBACK) {
  return call("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ redirect_uris: [redirect], token_endpoint_auth_method: "none", client_name: "Claude" }),
  });
}

async function pkce() {
  const verifier = "v".repeat(64);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return { verifier, challenge };
}

/** Open the login page like a browser would: returns the form handle and cookie. */
async function openLogin() {
  const { client_id } = (await (await register()).json()) as { client_id: string };
  const { verifier, challenge } = await pkce();
  const qs = new URLSearchParams({
    response_type: "code",
    client_id,
    redirect_uri: CALLBACK,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state: "xyz",
    resource: `${ORIGIN}/mcp`,
  });
  const page = await call(`/authorize?${qs}`);
  expect(page.status).toBe(200);
  const html = await page.text();
  const handle = /name="handle" value="([^"]+)"/.exec(html)![1];
  const cookie = page.headers.getSetCookie().map((c: string) => c.split(";")[0]).join("; ");
  return { client_id, verifier, handle, cookie, html, headers: page.headers };
}

function submit(login: { handle: string; cookie: string }, username: string, password: string, ip = crypto.randomUUID()) {
  return call("/authorize", {
    method: "POST",
    headers: { Cookie: login.cookie, "Content-Type": "application/x-www-form-urlencoded", "CF-Connecting-IP": ip },
    body: new URLSearchParams({ handle: login.handle, username, password }),
    redirect: "manual",
  });
}

async function signIn() {
  const login = await openLogin();
  const res = await submit(login, GOOD.username, GOOD.password);
  expect(res.status).toBe(302);
  const loc = new URL(res.headers.get("Location")!);
  expect(loc.origin + loc.pathname).toBe(CALLBACK);
  expect(loc.searchParams.get("state")).toBe("xyz");
  const token = await call("/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: loc.searchParams.get("code")!,
      redirect_uri: CALLBACK,
      client_id: login.client_id,
      code_verifier: login.verifier,
      resource: `${ORIGIN}/mcp`,
    }),
  });
  expect(token.status).toBe(200);
  return { ...((await token.json()) as { access_token: string; refresh_token: string }), client_id: login.client_id };
}

async function mcp(token: string | null, method: string, params: object = {}) {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await call("/mcp", { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  if (res.status !== 200) return { status: res.status, body: null as any };
  const text = await res.text();
  const json = text.startsWith("{") ? JSON.parse(text) : JSON.parse(/^data: (.*)$/m.exec(text)![1]);
  return { status: 200, body: json };
}

async function tool(token: string, name: string, args: object) {
  const { body } = await mcp(token, "tools/call", { name, arguments: args });
  const text = body.result.content[0].text as string;
  return { isError: !!body.result.isError, text, data: body.result.isError ? null : JSON.parse(text) };
}

describe("sign-in", () => {
  it("full OAuth flow gives a token that works, and nothing works without one", async () => {
    const { access_token } = await signIn();
    expect((await mcp(null, "tools/list")).status).toBe(401);
    const list = await mcp(access_token, "tools/list");
    expect(list.body.result.tools.map((t: any) => t.name).sort()).toEqual(
      ["describe_archive", "get_document", "get_document_text", "list_archives", "search", "status"],
    );
  });

  it("wrong password shows the form again and the same form still works afterwards", async () => {
    const login = await openLogin();
    const bad = await submit(login, GOOD.username, "falsch");
    expect(bad.status).toBe(401);
    expect(await bad.text()).toContain("Benutzername oder Passwort falsch");
    expect((await submit(login, GOOD.username, GOOD.password)).status).toBe(302);
  });

  it("refuses to register apps that are not Claude", async () => {
    expect((await register("https://evil.example/cb")).status).toBe(400);
    expect((await register("https://claude.ai.evil.example/api/mcp/auth_callback")).status).toBe(400);
  });

  it("login page cannot be framed and escapes what the user typed", async () => {
    const login = await openLogin();
    const frame = `${login.headers.get("X-Frame-Options")} ${login.headers.get("Content-Security-Policy")}`;
    expect(frame).toMatch(/DENY|frame-ancestors 'none'/);
    const res = await submit(login, '"><script>alert(1)</script>', "x");
    const html = await res.text();
    expect(html).not.toContain("<script>alert(1)</script>");
    expect(html).toContain("&#60;script&#62;");
  });

  it("form handle without the browser cookie is rejected (no CSRF)", async () => {
    const login = await openLogin();
    const res = await submit({ ...login, cookie: "" }, GOOD.username, GOOD.password);
    expect(res.status).toBe(400);
  });

  it("an auth code works once", async () => {
    const login = await openLogin();
    const loc = new URL((await submit(login, GOOD.username, GOOD.password)).headers.get("Location")!);
    const exchange = () =>
      call("/token", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "authorization_code",
          code: loc.searchParams.get("code")!,
          redirect_uri: CALLBACK,
          client_id: login.client_id,
          code_verifier: login.verifier,
          resource: `${ORIGIN}/mcp`,
        }),
      });
    expect((await exchange()).status).toBe(200);
    expect((await exchange()).status).toBe(400);
  });

  it("refresh signs in to DocuWare again, and fails once the password changed", async () => {
    const { refresh_token, client_id } = await signIn();
    const refresh = (rt: string) =>
      call("/token", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: rt, client_id }),
      });
    const ok = await refresh(refresh_token);
    expect(ok.status).toBe(200);
    const next = ((await ok.json()) as { refresh_token: string }).refresh_token;
    dw.password = "neu";
    expect((await refresh(next)).status).not.toBe(200);
  });

  it("rate-limits password guessing per IP", { timeout: 30_000 }, async () => {
    const login = await openLogin();
    const statuses = [];
    for (let i = 0; i < 12; i++) statuses.push((await submit(login, GOOD.username, "falsch", "9.9.9.9")).status);
    expect(statuses).toContain(429);
  });
});

describe("tools", () => {
  it("lists archives without baskets", async () => {
    const { access_token } = await signIn();
    const { data } = await tool(access_token, "list_archives", {});
    expect(data).toEqual([{ name: "Archiv", id: "fc1", organization: "Acme" }]);
  });

  it("search escapes values, sends ranges, hides system fields and formats dates", async () => {
    const { access_token } = await signIn();
    const { data } = await tool(access_token, "search", {
      archive: "archiv",
      filters: { Belegart: "Rechnung (eingehend)*", Betrag: { between: [10, null] } },
      order_by: [{ field: "Datum", direction: "desc" }],
    });
    const sent = JSON.parse(dw.calls.find((c) => c.url.includes("DialogExpression"))!.body!);
    expect(sent.Condition).toEqual([
      { DBName: "DOCTYPE", Value: ["Rechnung \\(eingehend\\)\\*"] },
      { DBName: "AMOUNT", Value: ["10", null] },
    ]);
    expect(sent.SortOrder).toEqual([{ Field: "DOCDATE", Direction: "Desc" }]);
    expect(data.items).toEqual([
      { id: "42", title: "Rechnung (Müller)", content_type: "application/pdf", fields: { Belegart: "Rechnung", Datum: "2024-01-31" } },
    ]);
  });

  it("rejects bad filters with a helpful message", async () => {
    const { access_token } = await signIn();
    const r = await tool(access_token, "search", { archive: "Archiv", filters: { Betrag: { like: "1*" } } });
    expect(r.isError).toBe(true);
    expect(r.text).toContain("does not support like");
  });

  it("document ids cannot inject paths into DocuWare URLs", async () => {
    const { access_token } = await signIn();
    for (const id of ["../../Organizations", "42/Sections", "42?x=1", "%2e%2e"]) {
      const r = await tool(access_token, "get_document", { archive: "Archiv", document_id: id });
      expect(r.isError).toBe(true);
    }
    expect(dw.calls.some((c) => c.url.includes("/Documents/") && !c.url.endsWith("/Documents/42"))).toBe(false);
  });

  it("OCR text is budgeted and table cells are included", async () => {
    const { access_token } = await signIn();
    const full = await tool(access_token, "get_document_text", { archive: "Archiv", document_id: "42" });
    expect(full.data.attachments[0].text).toBe("Hallo Welt\nZelle");
    const cut = await tool(access_token, "get_document_text", { archive: "Archiv", document_id: "42", max_chars: 5 });
    expect(cut.data.attachments[0]).toMatchObject({ text: "Hallo", truncated: true, char_count: 16 });
  });

  it("never sends the DocuWare token to a host other than DocuWare", async () => {
    const { access_token } = await signIn();
    dw.evilLink = true;
    const r = await tool(access_token, "get_document_text", { archive: "Archiv", document_id: "42" });
    expect(r.isError).toBe(true);
    expect(dw.calls.some((c) => c.url.startsWith("https://evil.test"))).toBe(false);
  });
});
