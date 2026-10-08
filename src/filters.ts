// Filter DSL → DocuWare DialogExpression conditions. Port of docuware_mcp/filters.py.
//   bare value or {eq}: exact · {like}: * and ? wildcards · {gte}/{lte}/{between}: inclusive
//   ranges (null = open bound) · null or {empty: true}: field is empty.

export interface Field {
  name: string;
  id: string;
  type: string | null;
  length: number;
  operators: string[];
  select_list?: string[];
}

const TEXT = ["eq", "like", "empty"];
const RANGE = ["eq", "gte", "lte", "between", "empty"];
const OPERATORS: Record<string, string[]> = {
  text: TEXT,
  memo: TEXT,
  keyword: TEXT,
  keywords: TEXT,
  numeric: RANGE,
  int: RANGE,
  decimal: RANGE,
  date: RANGE,
  datetime: RANGE,
};

export function operatorsFor(type: string | null): string[] {
  const key = (type ?? "").toLowerCase();
  return Object.hasOwn(OPERATORS, key) ? OPERATORS[key] : ["eq", "empty"];
}

export class FilterError extends Error {}

export function findField(fields: Field[], name: string): Field {
  const n = name.toLowerCase();
  const f = fields.find((f) => f.name.toLowerCase() === n || f.id.toLowerCase() === n);
  if (!f) throw new FilterError(`Unknown field ${JSON.stringify(name)}. Available: ${fields.map((f) => f.name).join(", ")}`);
  return f;
}

/** Backslash-escape DocuWare metacharacters; already-escaped sequences are kept (idempotent). */
export function escape(value: string, wildcards: boolean): string {
  const chars = wildcards ? "()*?" : "()";
  let out = "";
  for (let i = 0; i < value.length; i++) {
    const c = value[i];
    if (c === "\\") {
      // An escape we recognise stays as it is (idempotent); any other backslash is made literal.
      out += i + 1 < value.length && "()*?\\".includes(value[i + 1]) ? c + value[++i] : "\\\\";
      continue;
    }
    out += (chars.includes(c) ? "\\" : "") + c;
  }
  return out;
}

function coerce(value: unknown, f: Field): string {
  const type = (f.type ?? "").toLowerCase();
  const bad = (what: string) => new FilterError(`Field ${JSON.stringify(f.name)} is type ${f.type} — ${what}`);
  if (type === "date" || type === "datetime") {
    if (typeof value !== "string" || Number.isNaN(Date.parse(value))) throw bad(`expected an ISO date, got ${JSON.stringify(value)}`);
    return type === "date" ? value.slice(0, 10) : value;
  }
  if (type === "numeric" || type === "int" || type === "decimal") {
    const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
    if (typeof n !== "number" || !Number.isFinite(n) || (type !== "decimal" && !Number.isInteger(n)))
      throw bad(`expected a number, got ${JSON.stringify(value)}`);
    return String(n);
  }
  if (typeof value === "object" && value !== null) throw bad("expected a plain value");
  return escape(String(value), true);
}

function translate(f: Field, spec: unknown): (string | null)[] {
  const allow = (op: string) => {
    if (!f.operators.includes(op))
      throw new FilterError(`Field ${JSON.stringify(f.name)} (type=${f.type}) does not support ${op}. Allowed: ${f.operators.join(", ")}`);
  };
  if (spec === null) {
    allow("empty");
    return ["EMPTY()"];
  }
  if (typeof spec !== "object" || Array.isArray(spec)) {
    allow("eq");
    return [coerce(spec, f)];
  }
  const entries = Object.entries(spec as Record<string, unknown>);
  if (entries.length !== 1) throw new FilterError(`Field ${JSON.stringify(f.name)}: use exactly one operator, e.g. {"gte": 100}`);
  const [op, raw] = entries[0];
  allow(op);
  switch (op) {
    case "empty":
      return ["EMPTY()"];
    case "eq":
      return [coerce(raw, f)];
    case "like":
      if (typeof raw !== "string") throw new FilterError(`Field ${JSON.stringify(f.name)}: "like" needs a string`);
      return [escape(raw, false)];
    case "gte":
      return [coerce(raw, f), null];
    case "lte":
      return [null, coerce(raw, f)];
    case "between": {
      if (!Array.isArray(raw) || raw.length !== 2)
        throw new FilterError(`Field ${JSON.stringify(f.name)}: "between" needs [low, high] (null for an open bound)`);
      return raw.map((v) => (v === null ? null : coerce(v, f)));
    }
  }
  throw new FilterError(`Unsupported operator ${JSON.stringify(op)}`);
}

export function buildConditions(filters: Record<string, unknown>, fields: Field[]) {
  const seen = new Set<string>();
  return Object.entries(filters).map(([name, spec]) => {
    const f = findField(fields, name);
    if (seen.has(f.id)) throw new FilterError(`Field ${JSON.stringify(name)} appears twice`);
    seen.add(f.id);
    return { DBName: f.id, Value: translate(f, spec) };
  });
}

const DIRECTIONS: Record<string, string> = { asc: "Asc", desc: "Desc", default: "Default" };

export function buildSortOrder(orderBy: { field: string; direction?: string }[], fields: Field[]) {
  return orderBy.map(({ field, direction }) => {
    const key = (direction ?? "asc").toLowerCase();
    const dir = Object.hasOwn(DIRECTIONS, key) ? DIRECTIONS[key] : undefined;
    if (!dir) throw new FilterError(`direction must be asc, desc or default, got ${JSON.stringify(direction)}`);
    return { Field: findField(fields, field).id, Direction: dir };
  });
}
