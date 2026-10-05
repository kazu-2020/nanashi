// Pure calculations for the pivot grid, CSV and filters. No I/O here.
import {
  Aggregation,
  Display,
  type Members,
  type MetricDef,
  type QueryResponse,
  Role,
  type Value,
  ValueKind,
} from "./gen/nanashi/v1/plan_pb";

// METRIC is the pseudo-dimension that puts the Metrics on an axis.
export const METRIC = "#metric";

// Names gives the name of a list, a member or a Metric by id. The grid and the keys hold ids.
export type Names = Record<string, string>;

// label gives the name of an id for the screen. An id without a name (a removed object) shows as it is.
export const label = (names: Names, id: string) => names[id] ?? id;

// Filters is list id to member ids.
export type Filters = Record<string, string[]>;
// The member order (ids) of each list, and the Metric ids for METRIC.
export type Order = Record<string, string[]>;

export type Grid = {
  rowDims: string[];
  colDims: string[];
  rowKeys: string[][];
  colKeys: string[][];
  // The value of the cell at a row key and a column key.
  value: (r: string[], c: string[]) => Value | undefined;
  // The Metric of the cell at a row key and a column key.
  metric: (r: string[], c: string[]) => string;
};

// ponytail: the axis is the full product of the member lists, capped; add paging if users need more rows.
const MAX_KEYS = 2000;

function product(lists: string[][]): string[][] {
  return lists.reduce<string[][]>(
    (acc, l) => acc.flatMap((k) => l.map((m) => [...k, m])).slice(0, MAX_KEYS),
    [[]],
  );
}

function compareKeys(dims: string[], order: Order) {
  const rank = (d: string, m: string) => {
    if (m === "") return Number.MAX_SAFE_INTEGER;
    const i = (order[d] ?? []).indexOf(m);
    return i < 0 ? Number.MAX_SAFE_INTEGER - 1 : i;
  };
  return (a: string[], b: string[]) => {
    for (let i = 0; i < dims.length; i++) {
      const d = rank(dims[i], a[i]) - rank(dims[i], b[i]);
      if (d !== 0) return d;
      if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
    }
    return 0;
  };
}

function axisKeys(dims: string[], order: Order, filters: Filters, seen: string[][]): string[][] {
  const members = dims.map((d) => (filters[d]?.length ? filters[d] : (order[d] ?? [])));
  const all = new Map(product(members).map((k) => [JSON.stringify(k), k]));
  for (const k of seen) all.set(JSON.stringify(k), k);
  return [...all.values()].sort(compareKeys(dims, order));
}

// buildGrid places the cells of a Query response on rows and columns, in member order.
// rows and cols can contain METRIC. If they do not, metrics must have one Metric.
export function buildGrid(
  resp: QueryResponse,
  metrics: string[],
  rows: string[],
  cols: string[],
  order: Order,
  filters: Filters,
): Grid {
  const coordOf = (cell: QueryResponse["cells"][number], dim: string) =>
    dim === METRIC ? cell.metric : (cell.coords[resp.dimensions.indexOf(dim)] ?? "");
  const cellKey = (metric: string, coords: string[]) => JSON.stringify([metric, ...coords]);
  const cells = new Map(resp.cells.map((c) => [cellKey(c.metric, c.coords), c.value]));
  const ord = { ...order, [METRIC]: metrics };
  const rowKeys = axisKeys(
    rows,
    ord,
    filters,
    resp.cells.map((c) => rows.map((d) => coordOf(c, d))),
  );
  const colKeys = axisKeys(
    cols,
    ord,
    filters,
    resp.cells.map((c) => cols.map((d) => coordOf(c, d))),
  );
  const pick = (r: string[], c: string[], dim: string) => {
    const i = rows.indexOf(dim);
    return i >= 0 ? r[i] : (c[cols.indexOf(dim)] ?? "");
  };
  const metric = (r: string[], c: string[]) =>
    rows.includes(METRIC) || cols.includes(METRIC) ? pick(r, c, METRIC) : metrics[0];
  return {
    rowDims: rows,
    colDims: cols,
    rowKeys,
    colKeys,
    metric,
    value: (r, c) =>
      cells.get(
        cellKey(
          metric(r, c),
          resp.dimensions.map((d) => pick(r, c, d)),
        ),
      ),
  };
}

// formatValue gives the text of a value. A member value is an id: the text is its name.
export function formatValue(v: Value | undefined, names: Names): string {
  switch (v?.value.case) {
    case "number":
      return String(Math.round(v.value.value * 1e6) / 1e6);
    case "boolean":
      return v.value.value ? "TRUE" : "FALSE";
    case "member":
      return label(names, v.value.value);
    default:
      return "";
  }
}

// parseValue reads typed text for a Metric kind. Blank text gives undefined (a blank cell).
export function parseValue(kind: ValueKind, text: string): Value["value"] | undefined {
  const t = text.trim();
  if (t === "") return undefined;
  if (kind === ValueKind.BOOLEAN) {
    if (!/^(true|false|1|0)$/i.test(t)) throw new Error(`TRUE か FALSE ではありません: ${t}`);
    return { case: "boolean", value: /^(true|1)$/i.test(t) };
  }
  if (kind === ValueKind.MEMBER) return { case: "member", value: t };
  const n = Number(t);
  if (Number.isNaN(n)) throw new Error(`数値ではありません: ${t}`);
  return { case: "number", value: n };
}

// writeCoords gives the coordinates of an edit. A dimension that is not on an axis and not
// filtered to one member stays open, so the api spreads the value over it.
export function writeCoords(
  metricDims: string[],
  grid: Grid,
  r: string[],
  c: string[],
  filters: Filters,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const d of metricDims) {
    const i = grid.rowDims.indexOf(d);
    const j = grid.colDims.indexOf(d);
    const m = i >= 0 ? r[i] : j >= 0 ? c[j] : filters[d]?.length === 1 ? filters[d][0] : "";
    if (m !== "") out[d] = m;
  }
  return out;
}

// editable tells if an edit of a cell writes the typed value. A spread goes over all members of an open
// dimension, so a filter with more members must not leave the dimension open. Only SUM is the sum of the spread.
export function editable(
  def: Pick<MetricDef, "dimensions" | "formula" | "overridable"> | undefined,
  grid: Grid,
  filters: Filters,
  aggregation: Aggregation,
): boolean {
  if (!def || (def.formula && !def.overridable)) return false;
  if (aggregation !== Aggregation.UNSPECIFIED && aggregation !== Aggregation.SUM) return false;
  const onAxis = (d: string) => grid.rowDims.includes(d) || grid.colDims.includes(d);
  return def.dimensions.every((d) => onAxis(d) || (filters[d]?.length ?? 0) <= 1);
}

export const keyLabel = (k: string[], names: Names) =>
  k.map((m) => (m ? label(names, m) : "(なし)")).join(" / ");

function csvField(s: string): string {
  return /[",\n\r]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
}

export function gridToCsv(g: Grid, names: Names): string {
  const header = [
    ...g.rowDims.map((d) => label(names, d)),
    ...g.colKeys.map((c) => keyLabel(c, names) || "値"),
  ];
  const lines = g.rowKeys.map((r) => [
    ...r.map((m) => label(names, m)),
    ...g.colKeys.map((c) => formatValue(g.value(r, c), names)),
  ]);
  return [header, ...lines].map((l) => l.map(csvField).join(",")).join("\n") + "\n";
}

// parseCsv reads CSV text (RFC 4180 quotes) into rows of fields.
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') field += text[++i];
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += ch;
  }
  if (field !== "" || row.length) rows.push([...row, field]);
  return rows;
}

// mergeFilters puts the page selector members over the filters of a view.
export function mergeFilters(view: Filters, page: Record<string, string>): Filters {
  const out = { ...view };
  for (const [d, m] of Object.entries(page)) if (m) out[d] = [m];
  return out;
}

// chartRows gives one object per row key with one number per column key, for recharts.
export function chartRows(
  g: Grid,
  names: Names,
): { series: string[]; data: Record<string, string | number>[] } {
  const series = g.colKeys.map((c) => keyLabel(c, names) || "値");
  const data = g.rowKeys.map((r) => {
    const o: Record<string, string | number> = { name: keyLabel(r, names) || "合計" };
    g.colKeys.forEach((c, i) => {
      const v = g.value(r, c);
      if (v?.value.case === "number") o[series[i]] = v.value.value;
    });
    return o;
  });
  return { series, data };
}

export function total(g: Grid): number {
  let s = 0;
  for (const r of g.rowKeys)
    for (const c of g.colKeys) {
      const v = g.value(r, c);
      if (v?.value.case === "number") s += v.value.value;
    }
  return s;
}

export const ROLES: [string, string][] = [
  [String(Role.VIEWER), "閲覧者"],
  [String(Role.CONTRIBUTOR), "入力者"],
  [String(Role.MODELER), "モデラー"],
  [String(Role.ADMIN), "管理者"],
];

export const roleName = (r: Role) => ROLES.find(([v]) => v === String(r))?.[1] ?? "";

export type Spec = {
  metrics: string[];
  rows: string[];
  columns: string[];
  filters: Filters;
  aggregation: Aggregation;
  display: Display;
};

export const toFilters = (f: { [k: string]: Members }): Filters =>
  Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.ids]));
export const toProtoFilters = (f: Filters) =>
  Object.fromEntries(
    Object.entries(f)
      .filter(([, v]) => v.length)
      .map(([k, v]) => [k, { ids: v }]),
  );

// defaultSpec puts the Metrics (if more than one) and the first list on rows, and the second on columns. All ids.
export function defaultSpec(metrics: string[], dims: string[]): Spec {
  const many = metrics.length > 1;
  return {
    metrics,
    rows: many ? [METRIC] : dims.slice(0, 1),
    columns: many ? dims.slice(0, 1) : dims.slice(1, 2),
    filters: {},
    aggregation: Aggregation.SUM,
    display: Display.GRID,
  };
}

// uuidv7 makes a UUIDv7 (RFC 9562) from a Unix time in milliseconds and 16 random bytes.
// The time fills the first 48 bits, so an id from a later millisecond sorts after an id from an earlier one.
export function uuidv7(ms: number, random: Uint8Array): string {
  const b = random.slice(0, 16);
  for (let i = 0; i < 6; i++) b[i] = Math.floor(ms / 2 ** (8 * (5 - i))) % 256;
  b[6] = (b[6] & 0x0f) | 0x70; // version 7
  b[8] = (b[8] & 0x3f) | 0x80; // variant 10
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

// followOpId gives the client_op_id of the second write of one user action. It adds 1 to the last 48 bits of
// id, so a retry of the action gives the same value again, and the value is not id.
export const followOpId = (id: string) =>
  id.slice(0, 24) + ((parseInt(id.slice(24), 16) + 1) % 2 ** 48).toString(16).padStart(12, "0");

// dropsInputCells tells if saving next over the input Metric old deletes its cells: the engine makes the Metric again.
export const dropsInputCells = (
  old: Pick<MetricDef, "formula" | "kind" | "memberList" | "dimensions">,
  next: Pick<MetricDef, "formula" | "kind" | "memberList" | "dimensions">,
) =>
  old.formula === "" &&
  (next.formula !== "" ||
    next.kind !== old.kind ||
    next.memberList !== old.memberList ||
    next.dimensions.join("\n") !== old.dimensions.join("\n"));
