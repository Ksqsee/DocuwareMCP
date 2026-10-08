// A tiny in-memory DocuWare Cloud, enough for the endpoints the Worker calls.
export const DW = "https://dw.test";
export const GOOD = { username: "anna", password: "richtig" };

export interface FakeState {
  calls: { method: string; url: string; auth: string | null; body?: string }[];
  password: string;
  evilLink: boolean;
}

export function newState(): FakeState {
  return { calls: [], password: GOOD.password, evilLink: false };
}

const link = (rel: string, href: string) => ({ rel, href });

export function fakeDocuWare(state: FakeState) {
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const req = new Request(input, init);
    const url = new URL(req.url);
    const body = req.method === "POST" ? await req.text() : undefined;
    state.calls.push({ method: req.method, url: req.url, auth: req.headers.get("Authorization"), body });
    const json = (data: unknown, status = 200) => Response.json(data, { status });
    const p = url.pathname;

    if (url.origin !== DW) return new Response("not reachable", { status: 599 });
    if (p === "/DocuWare/Platform/Home/IdentityServiceInfo") return json({ IdentityServiceUrl: `${DW}/DocuWare/Identity` });
    if (p === "/DocuWare/Identity/.well-known/openid-configuration")
      return json({ token_endpoint: `${DW}/DocuWare/Identity/connect/token` });
    if (p === "/DocuWare/Identity/connect/token") {
      const f = new URLSearchParams(body);
      if (f.get("grant_type") !== "password" || f.get("username") !== GOOD.username || f.get("password") !== state.password)
        return json({ error: "invalid_grant" }, 400);
      return json({ access_token: "dw-token", expires_in: 3600 });
    }
    if (req.headers.get("Authorization") !== "Bearer dw-token") return new Response("unauthorized", { status: 401 });

    const P = `${DW}/DocuWare/Platform`;
    if (p === "/DocuWare/Platform") return json({ Links: [link("organizations", `${P}/Organizations`)] });
    if (p === "/DocuWare/Platform/Organizations")
      return json({ Organization: [{ Name: "Acme", Links: [link("filecabinets", `${P}/FileCabinets`)] }] });
    if (p === "/DocuWare/Platform/FileCabinets")
      return json({
        FileCabinet: [
          { Id: "fc1", Name: "Archiv", Links: [link("dialogs", `${P}/FileCabinets/fc1/Dialogs`), link("documents", `${P}/FileCabinets/fc1/Documents`)] },
          { Id: "b1", Name: "Inbox", IsBasket: true, Links: [] },
        ],
      });
    if (p === "/DocuWare/Platform/FileCabinets/fc1/Dialogs")
      return json({ Dialog: [{ $type: "DialogInfo", Id: "d1", Type: "Search", IsDefault: true, Links: [link("self", `${P}/FileCabinets/fc1/Dialogs/d1`)] }] });
    if (p === "/DocuWare/Platform/FileCabinets/fc1/Dialogs/d1")
      return json({
        Fields: [
          { DBFieldName: "DOCTYPE", DlgLabel: "Belegart", DWFieldType: "Text", Length: 40 },
          { DBFieldName: "AMOUNT", DlgLabel: "Betrag", DWFieldType: "Decimal" },
          { DBFieldName: "DOCDATE", DlgLabel: "Datum", DWFieldType: "Date" },
        ],
        Query: { Links: [link("dialogExpression", `${P}/FileCabinets/fc1/Query/DialogExpression?dialogId=d1`)] },
      });
    if (p === "/DocuWare/Platform/FileCabinets/fc1/Query/DialogExpression")
      return json({
        Count: { Value: 1 },
        Items: [
          {
            Id: 42,
            Title: "Rechnung (Müller)",
            ContentType: "application/pdf",
            Fields: [
              { FieldName: "DOCTYPE", Item: "Rechnung", ItemElementName: "String" },
              // 2024-01-31 00:00 in Berlin (UTC+1)
              { FieldName: "DOCDATE", Item: "/Date(1706655600000)/", ItemElementName: "Date" },
              { FieldName: "DWSYSTEM", Item: "secret system field", ItemElementName: "String" },
            ],
          },
        ],
        Links: [],
      });
    if (p === "/DocuWare/Platform/FileCabinets/fc1/Documents/42")
      return json({
        Id: 42,
        Title: "Rechnung (Müller)",
        Fields: [{ FieldName: "AMOUNT", Item: 99.5, ItemElementName: "Decimal" }],
        Sections: [
          {
            Id: "s1",
            OriginalFileName: "a.pdf",
            PageCount: 1,
            Links: [link("textshot", state.evilLink ? "https://evil.test/steal" : `${P}/FileCabinets/fc1/Sections/s1/Textshot`)],
          },
        ],
      });
    if (p === "/DocuWare/Platform/FileCabinets/fc1/Sections/s1/Textshot")
      return json({
        Pages: [
          {
            Items: [
              { $type: "TextZone", Ln: [{ Items: [{ $type: "Word", Value: "Hallo" }, { $type: "Word", Value: "Welt" }] }] },
              { $type: "TableZone", Cz: [{ TextZone: { Ln: [{ Items: [{ $type: "Word", Value: "Zelle" }] }] } }] },
            ],
          },
        ],
      });
    return new Response("not found", { status: 404 });
  };
}
