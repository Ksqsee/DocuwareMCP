// Minimal read-only DocuWare Platform REST client, ported from sniner/docuware-client.

export interface Creds {
  username: string;
  password: string;
}

export class DocuWareError extends Error {
  constructor(
    message: string,
    readonly status = 0,
  ) {
    super(message);
  }
}

type Json = Record<string, any>;

// DocuWare's JSON casing is not guaranteed, so read keys case-insensitively.
export function pick(obj: Json | undefined, key: string): any {
  if (!obj) return undefined;
  if (key in obj) return obj[key];
  const lower = key.toLowerCase();
  for (const k of Object.keys(obj)) if (k.toLowerCase() === lower) return obj[k];
  return undefined;
}

export function links(obj: Json | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const l of pick(obj, "Links") ?? []) out[String(pick(l, "rel")).toLowerCase()] = pick(l, "href");
  return out;
}

const JSON_HEADERS = { Accept: "application/json" };

/** Log in with the password grant; returns a DocuWare access token and its lifetime. */
export async function login(base: string, creds: Creds): Promise<{ token: string; expiresIn: number }> {
  const info = await getJson(`${base}/DocuWare/Platform/Home/IdentityServiceInfo`);
  const identity = String(pick(info, "IdentityServiceUrl") ?? "").replace(/\/$/, "");
  const oidc = await getJson(`${identity}/.well-known/openid-configuration`);
  const tokenUrl = pick(oidc, "token_endpoint") ?? `${base}/DocuWare/Identity/connect/token`;
  const resp = await fetch(tokenUrl, {
    method: "POST",
    headers: JSON_HEADERS,
    body: new URLSearchParams({
      grant_type: "password",
      username: creds.username,
      password: creds.password,
      client_id: "docuware.platform.net.client",
      scope: "docuware.platform",
    }),
  });
  if (resp.status === 400 || resp.status === 401) throw new DocuWareError("Benutzername oder Passwort falsch.", 401);
  if (!resp.ok) throw new DocuWareError(`DocuWare-Anmeldung fehlgeschlagen (HTTP ${resp.status}).`, resp.status);
  const body = (await resp.json()) as Json;
  if (!body.access_token) throw new DocuWareError("DocuWare hat kein Token geliefert.");
  return { token: body.access_token, expiresIn: Number(body.expires_in) || 3600 };
}

async function getJson(url: string): Promise<Json> {
  const resp = await fetch(url, { headers: JSON_HEADERS });
  if (!resp.ok) throw new DocuWareError(`DocuWare nicht erreichbar (HTTP ${resp.status}).`, resp.status);
  return resp.json();
}

export class DocuWare {
  constructor(
    readonly base: string,
    private creds: Creds,
    private token?: string,
  ) {}

  private url(href: string): string {
    const url = new URL(href, this.base);
    // Links come from DocuWare, but never let a link send our token to another host.
    if (url.origin !== new URL(this.base).origin) throw new DocuWareError(`Unexpected link target: ${url.origin}`);
    return url.toString();
  }

  private async request(href: string, init: RequestInit = {}): Promise<Response> {
    const send = () =>
      fetch(this.url(href), {
        ...init,
        headers: { ...JSON_HEADERS, ...(init.headers ?? {}), Authorization: `Bearer ${this.token}` },
      });
    if (!this.token) this.token = (await login(this.base, this.creds)).token;
    let resp = await send();
    if (resp.status === 401 || resp.status === 403) {
      this.token = (await login(this.base, this.creds)).token;
      resp = await send();
    }
    if (!resp.ok) {
      const text = (await resp.text()).slice(0, 300);
      throw new DocuWareError(`DocuWare HTTP ${resp.status}: ${text}`, resp.status);
    }
    return resp;
  }

  get(href: string): Promise<Json> {
    return this.request(href).then((r) => r.json());
  }

  post(href: string, body: unknown): Promise<Json> {
    return this.request(href, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json());
  }

  async cabinets(): Promise<Cabinet[]> {
    const platform = await this.get("/DocuWare/Platform");
    const orgs = await this.get(links(platform).organizations);
    const out: Cabinet[] = [];
    for (const org of pick(orgs, "Organization") ?? []) {
      const fcs = await this.get(links(org).filecabinets);
      for (const fc of pick(fcs, "FileCabinet") ?? []) {
        if (pick(fc, "IsBasket")) continue;
        out.push({ id: pick(fc, "Id"), name: pick(fc, "Name"), organization: pick(org, "Name"), links: links(fc) });
      }
    }
    return out;
  }

  async searchDialog(fc: Cabinet): Promise<Json> {
    const dialogs = pick(await this.get(fc.links.dialogs), "Dialog") ?? [];
    const search = dialogs.filter(
      (d: Json) => pick(d, "$type") === "DialogInfo" && pick(d, "Type") === "Search" && !String(pick(d, "Id")).includes("_"),
    );
    const info = search.find((d: Json) => pick(d, "IsDefault")) ?? search[0];
    if (!info) throw new DocuWareError(`Archiv "${fc.name}" hat keinen Suchdialog.`);
    return this.get(links(info).self);
  }
}

export interface Cabinet {
  id: string;
  name: string;
  organization: string;
  links: Record<string, string>;
}

const DATE_RE = /^\/Date\((-?\d+)\)\/$/;

/** Turn a DocuWare field value into plain JSON (dates as ISO strings). */
export function fieldValue(field: Json): unknown {
  const type = pick(field, "ItemElementName");
  const item = pick(field, "Item");
  if (item === null || item === undefined) return null;
  if (type === "Keywords") return pick(item, "Keyword") ?? [];
  const m = typeof item === "string" ? DATE_RE.exec(item) : null;
  if (m) {
    const ms = Number(m[1]);
    if (ms <= 0) return null;
    // A Date is local midnight on the DocuWare server; +12h lands inside that day in any zone.
    return type === "Date" ? new Date(ms + 12 * 3600_000).toISOString().slice(0, 10) : new Date(ms).toISOString();
  }
  return item;
}

/** Plain text of a DocuWare textshot: pages split by form feed, lines by newline. */
export function textshotText(shot: Json): string {
  const lineText = (ln: Json) =>
    (pick(ln, "Items") ?? [])
      .filter((w: Json) => pick(w, "$type") === "Word" && pick(w, "Value"))
      .map((w: Json) => pick(w, "Value"))
      .join(" ");
  const zoneText = (z: Json): string => (pick(z, "Ln") ?? []).map(lineText).join("\n");
  return (pick(shot, "Pages") ?? [])
    .map((page: Json) =>
      (pick(page, "Items") ?? [])
        .map((z: Json) => {
          if (pick(z, "$type") === "TextZone") return zoneText(z);
          if (pick(z, "$type") === "TableZone")
            return (pick(z, "Cz") ?? [])
              .map((c: Json) => zoneText(pick(c, "TextZone") ?? {}))
              .filter(Boolean)
              .join("\n");
          return "";
        })
        .join("\n"),
    )
    .join("\f");
}
