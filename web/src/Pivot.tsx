// The pivot of Metrics: axes, filters, grid with cell edits, charts, CSV export, views and cell comments.
import { Button, Input } from "@heroui/react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, getUser } from "./api";
import { Display, Role } from "./gen/nanashi/v1/plan_pb";
import {
  buildGrid,
  chartRows,
  dimLabel,
  formatValue,
  gridToCsv,
  keyLabel,
  METRIC,
  mergeFilters,
  parseValue,
  total,
  spreadable,
  writeCoords,
  type Grid,
  type Spec,
  toProtoFilters,
} from "./logic";
import { cls, time, useApp, useRun } from "./state";
import { Checks, Sel } from "./ui";

const DISPLAYS: [string, string][] = [
  [String(Display.GRID), "グリッド"],
  [String(Display.LINE), "折れ線"],
  [String(Display.BAR), "棒"],
  [String(Display.KPI), "KPI"],
];
const AGGS = ["SUM", "AVG", "MIN", "MAX", "COUNT"];

export function Pivot(props: {
  initial: Spec;
  // A board widget hides the controls and adds the page selector members.
  compact?: boolean;
  page?: Record<string, string>;
  view?: { id: string; name: string };
}) {
  const { appId, model, can, reload } = useApp();
  const run = useRun();
  const [spec, setSpec] = useState(props.initial);
  const [cell, setCell] = useState<{ metric: string; coords: Record<string, string> }>();
  const view = props.view;
  const [viewName, setViewName] = useState(view?.name ?? "");
  const order = useMemo(
    () => Object.fromEntries(model.lists.map((l) => [l.name, l.members.map((m) => m.name)])),
    [model],
  );
  const defs = new Map(model.metrics.map((m) => [m.name, m]));
  const dims = [...new Set(spec.metrics.flatMap((m) => defs.get(m)?.dimensions ?? []))];
  const axisDims = spec.metrics.length > 1 ? [METRIC, ...dims] : dims;
  const filters = mergeFilters(spec.filters, props.page ?? {});
  const strip = (ds: string[]) => ds.filter((d) => d !== METRIC);

  const {
    data: resp,
    refetch: requery,
    isPlaceholderData,
  } = useQuery({
    queryKey: ["query", appId, String(model.seq), spec, props.page],
    queryFn: () =>
      api.query({
        appId,
        metrics: spec.metrics,
        rows: strip(spec.rows),
        columns: strip(spec.columns),
        filters: toProtoFilters(filters),
        aggregation: spec.aggregation,
      }),
    // The grid keeps the old values while it reads the new ones. The old cells are read-only then:
    // a write would use the new axes and filters.
    placeholderData: keepPreviousData,
  });
  const grid = resp && buildGrid(resp, spec.metrics, spec.rows, spec.columns, order, filters);

  const axisOf = (d: string) =>
    spec.rows.includes(d) ? "rows" : spec.columns.includes(d) ? "columns" : "";
  const setAxis = (d: string, axis: string) => {
    const rows = spec.rows.filter((x) => x !== d);
    const columns = spec.columns.filter((x) => x !== d);
    if (axis === "rows") rows.push(d);
    if (axis === "columns") columns.push(d);
    setSpec({ ...spec, rows, columns });
  };

  const write = (g: Grid, r: string[], c: string[], text: string) => {
    const metric = g.metric(r, c);
    const def = defs.get(metric)!;
    const coords = writeCoords(def.dimensions, g, r, c, filters);
    return run(async () => {
      const v = parseValue(def.kind, text);
      await api.writeCells({
        appId,
        writes: [{ metric, coords, value: v ? { value: v } : undefined }],
      });
      await requery();
    });
  };

  const saveView = (id: string) =>
    run(async () => {
      await api.saveView({
        appId,
        id,
        name: viewName,
        ...spec,
        filters: toProtoFilters(spec.filters),
      });
      await reload();
    });

  const exportCsv = (g: Grid) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([gridToCsv(g)], { type: "text/csv" }));
    a.download = `${spec.metrics.join("_")}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 0);
  };

  return (
    <div className="flex flex-col gap-3">
      {!props.compact && (
        <div className="flex flex-col gap-2 rounded border p-2">
          <div className="flex flex-wrap gap-3">
            {axisDims.map((d) => (
              <Sel
                key={d}
                label={dimLabel(d)}
                value={axisOf(d)}
                onChange={(a) => setAxis(d, a)}
                empty="なし"
                options={[
                  ["rows", "行"],
                  ["columns", "列"],
                ]}
              />
            ))}
            <Sel
              label="集計"
              value={spec.aggregation}
              onChange={(aggregation) => setSpec({ ...spec, aggregation })}
              options={AGGS}
            />
            <Sel
              label="表示"
              value={String(spec.display || Display.GRID)}
              onChange={(v) => setSpec({ ...spec, display: Number(v) })}
              options={DISPLAYS}
            />
          </div>
          <details>
            <summary className="cursor-pointer text-sm">フィルター（ページセレクター）</summary>
            {dims.map((d) => (
              <div key={d} className="flex gap-2">
                <span className="w-24 text-sm font-bold">{d}</span>
                <Checks
                  label={d}
                  options={order[d] ?? []}
                  value={spec.filters[d] ?? []}
                  onChange={(v) => setSpec({ ...spec, filters: { ...spec.filters, [d]: v } })}
                />
              </div>
            ))}
          </details>
          <div className="flex flex-wrap items-center gap-2">
            {grid && (
              <Button size="sm" variant="secondary" onPress={() => exportCsv(grid)}>
                CSV エクスポート
              </Button>
            )}
            {can(Role.MODELER) && (
              <>
                <Input
                  aria-label="ビュー名"
                  placeholder="ビュー名"
                  value={viewName}
                  onChange={(e) => setViewName(e.target.value)}
                />
                <Button size="sm" variant="secondary" onPress={() => saveView("")}>
                  ビューとして保存
                </Button>
                {view && (
                  <Button size="sm" variant="secondary" onPress={() => saveView(view.id)}>
                    このビューを上書き保存
                  </Button>
                )}
              </>
            )}
          </div>
        </div>
      )}
      {!grid ? (
        <p className="text-sm">読み込み中…</p>
      ) : (
        <Body
          grid={grid}
          display={spec.display}
          editable={(m) => {
            const d = defs.get(m);
            return (
              !props.compact &&
              !isPlaceholderData &&
              can(Role.CONTRIBUTOR) &&
              !!d &&
              (!d.formula || d.overridable) &&
              spreadable(d.dimensions, grid, filters, spec.aggregation)
            );
          }}
          onWrite={(r, c, t) => write(grid, r, c, t)}
          onSelect={(r, c) => {
            const metric = grid.metric(r, c);
            const d = defs.get(metric);
            if (d) setCell({ metric, coords: writeCoords(d.dimensions, grid, r, c, filters) });
          }}
        />
      )}
      {cell && !props.compact && <CellComments {...cell} />}
    </div>
  );
}

function Body(props: {
  grid: Grid;
  display: Display;
  editable: (metric: string) => boolean;
  onWrite: (r: string[], c: string[], text: string) => Promise<boolean>;
  onSelect: (r: string[], c: string[]) => void;
}) {
  const g = props.grid;
  if (props.display === Display.KPI)
    return <p className="text-4xl font-bold">{total(g).toLocaleString("ja-JP")}</p>;
  if (props.display === Display.LINE || props.display === Display.BAR) {
    const { series, data } = chartRows(g);
    const Chart = props.display === Display.LINE ? LineChart : BarChart;
    const colors = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2"];
    return (
      <div className="h-72 w-full">
        <ResponsiveContainer>
          <Chart data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="name" />
            <YAxis />
            <Tooltip />
            <Legend />
            {series.map((s, i) =>
              props.display === Display.LINE ? (
                <Line key={s} dataKey={s} stroke={colors[i % colors.length]} />
              ) : (
                <Bar key={s} dataKey={s} fill={colors[i % colors.length]} />
              ),
            )}
          </Chart>
        </ResponsiveContainer>
      </div>
    );
  }
  return (
    <div className="overflow-auto">
      <table className={cls.table}>
        <thead>
          <tr>
            {g.rowDims.map((d) => (
              <th key={d}>{dimLabel(d)}</th>
            ))}
            {g.colKeys.map((c) => (
              <th key={JSON.stringify(c)}>{keyLabel(c) || "値"}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {g.rowKeys.map((r) => (
            <tr key={JSON.stringify(r)}>
              {r.map((m, i) => (
                <th key={i} className="text-left">
                  {m || "(なし)"}
                </th>
              ))}
              {g.colKeys.map((c) => {
                const text = formatValue(g.value(r, c));
                return (
                  <td
                    key={JSON.stringify(c)}
                    className="text-right"
                    onClick={() => props.onSelect(r, c)}
                  >
                    {props.editable(g.metric(r, c)) ? (
                      <Input
                        key={text}
                        aria-label={`${keyLabel(r)} ${keyLabel(c) || "値"}`}
                        className="h-7 w-24 px-1 py-0 text-right"
                        defaultValue={text}
                        onBlur={(e) => {
                          const input = e.target;
                          if (input.value === text) return;
                          // A refused write must not leave the typed value in the grid.
                          void props.onWrite(r, c, input.value).then((ok) => {
                            if (!ok) input.value = text;
                          });
                        }}
                        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
                      />
                    ) : (
                      text
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const sameCell = (a: Record<string, string>, b: Record<string, string>) =>
  Object.keys(a).length === Object.keys(b).length &&
  Object.entries(a).every(([k, v]) => b[k] === v);

function CellComments(props: { metric: string; coords: Record<string, string> }) {
  const { appId } = useApp();
  const run = useRun();
  const target = `metric:${props.metric}`;
  const [body, setBody] = useState("");
  const { data: resp, refetch: again } = useQuery({
    queryKey: ["comments", appId, target],
    queryFn: () => api.listComments({ appId, target }),
  });
  const list = resp?.comments.filter((c) => sameCell(c.cell, props.coords)) ?? [];
  return (
    <div className="rounded border p-2 text-sm">
      <p className="font-bold">
        コメント: {props.metric} {JSON.stringify(props.coords)}
      </p>
      {list.map((c) => (
        <p key={c.id}>
          {c.user}（{time(c.createdAt)}）: {c.body}
        </p>
      ))}
      <div className="flex gap-2">
        <Input
          aria-label="コメント"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder={`${getUser()} としてコメント`}
        />
        <Button
          size="sm"
          onPress={() =>
            run(async () => {
              await api.addComment({ appId, target, cell: props.coords, body });
              setBody("");
              await again();
            })
          }
        >
          追加
        </Button>
      </div>
    </div>
  );
}
