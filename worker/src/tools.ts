import { McpServer } from "@modelcontextprotocol/server";
import { z } from "zod";
import { type Cabinet, type Creds, DocuWare, DocuWareError, fieldValue, links, pick, textshotText } from "./docuware";
import { buildConditions, buildSortOrder, type Field, FilterError, operatorsFor } from "./filters";

export interface Props extends Creds {
  dwToken?: string;
}

// Each keyword field's value list costs one request; Workers cap requests per call.
const MAX_SELECT_LISTS = 15;

// Some clients send nested arguments as JSON strings; accept both.
const jsonArg = <T extends z.ZodType>(schema: T) =>
  z.preprocess((v) => {
    if (typeof v !== "string") return v;
    try {
      return JSON.parse(v);
    } catch {
      return v;
    }
  }, schema);

async function resolve(dw: DocuWare, archive: string): Promise<Cabinet> {
  const all = await dw.cabinets();
  const byId = all.find((c) => c.id === archive);
  if (byId) return byId;
  const matches = all.filter((c) => c.name.toLowerCase() === archive.toLowerCase());
  if (matches.length === 1) return matches[0];
  if (matches.length > 1)
    throw new FilterError(`Archive name ${JSON.stringify(archive)} exists in several organizations; use the id: ${matches.map((c) => `${c.name} [id=${c.id}, org=${c.organization}]`).join("; ")}`);
  throw new FilterError(`Archive not found: ${JSON.stringify(archive)}. Call list_archives to see the names.`);
}

async function describe(dw: DocuWare, dialog: Record<string, any>, withLists: boolean): Promise<Field[]> {
  let lists = 0;
  const fields: Field[] = [];
  for (const f of pick(dialog, "Fields") ?? []) {
    const type = pick(f, "DWFieldType") ?? null;
    const field: Field = {
      name: pick(f, "DlgLabel") ?? pick(f, "DBFieldName"),
      id: pick(f, "DBFieldName"),
      type,
      length: pick(f, "Length") ?? -1,
      operators: operatorsFor(type),
    };
    const listUrl = links(f).simpleselectlist;
    if (withLists && listUrl && /^keywords?$/i.test(type ?? "") && lists++ < MAX_SELECT_LISTS) {
      const values = pick(await dw.get(listUrl).catch(() => ({})), "Value");
      if (values?.length) field.select_list = values.map(String);
    }
    fields.push(field);
  }
  return fields;
}

function fieldsToObject(values: Record<string, any>[] | undefined, fields: Field[]) {
  const byId = new Map(fields.map((f) => [f.id, f.name]));
  const out: Record<string, unknown> = {};
  for (const v of values ?? []) {
    const name = byId.get(pick(v, "FieldName"));
    if (name) out[name] = fieldValue(v);
  }
  return out;
}

async function loadDocument(dw: DocuWare, archive: string, documentId: string) {
  // Only digits: the id goes into a URL path, so nothing else may reach DocuWare.
  if (!/^\d{1,18}$/.test(documentId)) throw new FilterError("document_id must be the numeric DocuWare document id");
  const fc = await resolve(dw, archive);
  return { fc, doc: await dw.get(`${fc.links.documents}/${documentId}`) };
}

function attachmentSummary(s: Record<string, any>) {
  return {
    attachment_id: String(pick(s, "Id")),
    filename: pick(s, "OriginalFileName") ?? null,
    content_type: pick(s, "ContentType") ?? null,
    pages: pick(s, "PageCount") ?? null,
    size: pick(s, "FileSize") ?? null,
  };
}

const ok = (data: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(data, null, 1) }] });

// Errors the caller can fix go back as text; anything else stays generic.
async function run(fn: () => Promise<unknown>) {
  try {
    return ok(await fn());
  } catch (e) {
    const msg = e instanceof FilterError || e instanceof DocuWareError ? e.message : "Internal error";
    if (!(e instanceof FilterError)) console.error(e);
    return { isError: true, content: [{ type: "text" as const, text: msg }] };
  }
}

export function buildServer(base: string, props: Props): McpServer {
  const dw = new DocuWare(base, props, props.dwToken);
  const server = new McpServer({ name: "docuware", version: "1.0.0" });
  const readOnly = { readOnlyHint: true, openWorldHint: false };

  server.registerTool(
    "status",
    { description: "Check the DocuWare connection: who is signed in and how many archives they can see.", annotations: readOnly },
    () => run(async () => ({ connected: true, user: props.username, archive_count: (await dw.cabinets()).length })),
  );

  server.registerTool(
    "list_archives",
    { description: "List the DocuWare archives (file cabinets) the signed-in user can read. Baskets are excluded.", annotations: readOnly },
    () => run(async () => (await dw.cabinets()).map(({ name, id, organization }) => ({ name, id, organization }))),
  );

  server.registerTool(
    "describe_archive",
    {
      description: "Describe an archive's fields: names, types, which filter operators each accepts, and the allowed values of keyword fields. Call this before search.",
      inputSchema: { archive: z.string().describe("Archive name or id") },
      annotations: readOnly,
    },
    ({ archive }) =>
      run(async () => {
        const fc = await resolve(dw, archive);
        return { name: fc.name, id: fc.id, fields: await describe(dw, await dw.searchDialog(fc), true) };
      }),
  );

  server.registerTool(
    "search",
    {
      description: `Search documents in an archive.
filters: object mapping field names to a value (exact match) or one operator: {"like": "Müller*"} (wildcards * ?), {"gte": x}, {"lte": x}, {"between": [low, high]} (null = open), {"empty": true}. Dates as YYYY-MM-DD. Express every bound as a filter; order_by + limit only ranks.
order_by: [{"field": "...", "direction": "asc|desc"}]. Omit filters to list everything (use order_by + a small limit).`,
      inputSchema: {
        archive: z.string().describe("Archive name or id"),
        filters: jsonArg(z.record(z.string(), z.unknown())).optional(),
        combinator: z.enum(["AND", "OR"]).default("AND"),
        order_by: jsonArg(z.array(z.object({ field: z.string(), direction: z.string().optional() }))).optional(),
        limit: z.number().int().min(1).max(200).default(25),
        offset: z.number().int().min(0).max(1000).default(0),
      },
      annotations: readOnly,
    },
    ({ archive, filters, combinator, order_by, limit, offset }) =>
      run(async () => {
        const fc = await resolve(dw, archive);
        const dialog = await dw.searchDialog(fc);
        const fields = await describe(dw, dialog, false);
        const body: Record<string, unknown> = {
          Condition: buildConditions(filters ?? {}, fields),
          Operation: combinator === "OR" ? "Or" : "And",
        };
        if (order_by?.length) body.SortOrder = buildSortOrder(order_by, fields);
        let page = await dw.post(links(pick(dialog, "Query")).dialogexpression, body);
        const count = pick(pick(page, "Count"), "Value") ?? null;
        const items: unknown[] = [];
        let skipped = 0;
        while (items.length < limit) {
          for (const it of pick(page, "Items") ?? []) {
            if (skipped < offset) {
              skipped++;
              continue;
            }
            if (items.length >= limit) break;
            items.push({
              id: String(pick(it, "Id")),
              title: pick(it, "Title") ?? null,
              content_type: pick(it, "ContentType") ?? null,
              fields: fieldsToObject(pick(it, "Fields"), fields),
            });
          }
          const next = links(page).next;
          if (!next || items.length >= limit) break;
          page = await dw.get(next);
        }
        return { items, count, limit, offset };
      }),
  );

  server.registerTool(
    "get_document",
    {
      description: "Get one document's index fields and its list of attachments (no file content; use get_document_text for the text).",
      inputSchema: { archive: z.string(), document_id: z.string().describe("Numeric document id from search") },
      annotations: readOnly,
    },
    ({ archive, document_id }) =>
      run(async () => {
        const { fc, doc } = await loadDocument(dw, archive, document_id);
        const fields = await describe(dw, await dw.searchDialog(fc), false);
        return {
          id: String(pick(doc, "Id")),
          title: pick(doc, "Title") ?? null,
          content_type: pick(doc, "ContentType") ?? null,
          fields: fieldsToObject(pick(doc, "Fields"), fields),
          attachments: (pick(doc, "Sections") ?? []).map(attachmentSummary),
        };
      }),
  );

  server.registerTool(
    "get_document_text",
    {
      description: "Get the OCR text of a document's attachments (archive must be fulltext-indexed). Text beyond max_chars is cut and marked truncated.",
      inputSchema: {
        archive: z.string(),
        document_id: z.string(),
        attachment_id: z.string().optional(),
        max_chars: z.number().int().min(1).max(500_000).default(50_000),
      },
      annotations: readOnly,
    },
    ({ archive, document_id, attachment_id, max_chars }) =>
      run(async () => {
        const { doc } = await loadDocument(dw, archive, document_id);
        let sections: Record<string, any>[] = pick(doc, "Sections") ?? [];
        if (attachment_id !== undefined) {
          sections = sections.filter((s) => String(pick(s, "Id")) === attachment_id);
          if (!sections.length) throw new FilterError(`Document ${document_id} has no attachment ${attachment_id}`);
        }
        let remaining = max_chars;
        const attachments = [];
        for (const s of sections) {
          const entry: Record<string, unknown> = attachmentSummary(s);
          if (remaining <= 0) {
            entry.text = "";
            entry.truncated = true;
          } else {
            let shotUrl = links(s).textshot;
            if (!shotUrl && links(s).self) shotUrl = links(await dw.get(links(s).self)).textshot;
            if (!shotUrl) {
              entry.text = null;
              entry.error = "No OCR text (archive not fulltext-indexed or not processed yet)";
            } else {
              const text = textshotText(await dw.get(shotUrl));
              entry.text = text.slice(0, remaining);
              entry.char_count = text.length;
              entry.truncated = text.length > remaining;
              remaining -= (entry.text as string).length;
            }
          }
          attachments.push(entry);
        }
        return { document_id, attachments };
      }),
  );

  return server;
}
