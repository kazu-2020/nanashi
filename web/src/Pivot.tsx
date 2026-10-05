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
import { api, getUser, newId } from "./api";
import { Aggregation, Display, Role } from "./gen/nanashi/v1/plan_pb";
import {
  buildGrid,
  chartRows,
  editable,
  formatValue,
  gridToCsv,
  keyLabel,
  label,
  METRIC,
  mergeFilters,
  parseValue,
  total,
  writeCoords,
  type Grid,
  type Names,
  type Spec,
  toProtoFilters,
} from "./logic";
import { cls, createOrUpdate, memberOptions, time, useApp, useMutate } from "./state";
import { Checks, Sel } from "./ui";

const DISPLAYS: [string, string][] = [
  [String(Display.GRID), "グリッド"],
  [String(Display.LINE), "折れ線"],
  [String(Display.BAR), "棒"],
  [String(Display.KPI), "KPI"],
];
const AGGS = [
  Aggregation.SUM,
  Aggregation.AVG,
  Aggregation.MIN,
  Aggregation.MAX,
  Aggregation.COUNT,
].map((a): [string, string] => [String(a), Aggregation[a]]);

// page holds the page selector members of a board.
function usePivot(spec: Spec, page?: Record<string, string>) {
  const { appId, model } = useApp();
  const order = useMemo(
    () => Object.fromEntries(model.lists.map((l) => [l.id, l.members.map((m) => m.id)])),
    [model],
  );
  const defs = new Map(model.metrics.map((m) => [m.id, m]));
  const dims = [...new Set(spec.metrics.flatMap((m) => defs.get(m)?.dimensions ?? []))];
  const filters = mergeFilters(spec.filters, page ?? {});
  const strip = (ds: string[]) => ds.filter((d) => d !== METRIC);

  const { data: resp, isPlaceholderData } = useQuery({
    queryKey: ["app", appId, "query", spec, page],
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
  return { grid, filters, defs, dims, order, isPlaceholderData };
}

export function PivotWidget(props: { spec: Spec; page: Record<string, string> }) {
  const { names } = useApp();
  const { grid } = usePivot(props.spec, props.page);
  if (!grid) return <p className="text-sm">読み込み中…</p>;
  return <Body grid={grid} names={names} display={props.spec.display} editable={() => false} />;
}

export function PivotEditor(props: { initial: Spec; view?: { id: string; name: string } }) {
  const { appId, can, names, model } = useApp();
  const mutate = useMutate();
  const writeCells = useMutate("query");
  const [spec, setSpec] = useState(props.initial);
  const [cell, setCell] = useState<{ metric: string; coords: Record<string, string> }>();
  const view = props.view;
  const [viewName, setViewName] = useState(view?.name ?? "");
  const { grid, filters, defs, dims, order, isPlaceholderData } = usePivot(spec);
  const axisDims = spec.metrics.length > 1 ? [METRIC, ...dims] : dims;

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
    return writeCells(async (clientOpId) => {
      let v = parseValue(def.kind, text);
      // The user types the name of a member. The api takes its id.
      if (v?.case === "member") {
        const id = memberOptions(model, def.memberList).find(([, n]) => n === v?.value)?.[0];
        if (!id) throw new Error(`メンバーがありません: ${v.value}`);
        v = { case: "member", value: id };
      }
      await api.writeCells({
        appId,
        clientOpId,
        writes: [{ metric, coords, value: v ? { value: v } : undefined }],
      });
    });
  };

  const [draftId, setDraftId] = useState(newId);
  const saveView = (id?: string) =>
    mutate(async (clientOpId) => {
      const req = {
        appId,
        clientOpId,
        view: {
          id: id ?? draftId,
          name: viewName,
          ...spec,
          filters: toProtoFilters(spec.filters),
        },
      };
      if (id) return api.updateView(req);
      await createOrUpdate(
        req,
        (r) => api.createView(r),
        (r) => api.updateView(r),
      );
      setDraftId(newId());
    });

  const exportCsv = (g: Grid) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([gridToCsv(g, names)], { type: "text/csv" }));
    a.download = `${spec.metrics.map((m) => label(names, m)).join("_")}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 0);
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2 rounded border p-2">
        <div className="flex flex-wrap gap-3">
          {axisDims.map((d) => (
            <Sel
              key={d}
              label={label(names, d)}
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
            value={String(spec.aggregation || Aggregation.SUM)}
            onChange={(v) => setSpec({ ...spec, aggregation: Number(v) })}
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
              <span className="w-24 text-sm font-bold">{label(names, d)}</span>
              <Checks
                label={label(names, d)}
                options={(order[d] ?? []).map((m): [string, string] => [m, label(names, m)])}
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
              <Button size="sm" variant="secondary" onPress={() => saveView()}>
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
      {!grid ? (
        <p className="text-sm">読み込み中…</p>
      ) : (
        <Body
          grid={grid}
          names={names}
          display={spec.display}
          editable={(m) =>
            !isPlaceholderData &&
            can(Role.CONTRIBUTOR) &&
            editable(defs.get(m), grid, filters, spec.aggregation)
          }
          onWrite={(r, c, t) => write(grid, r, c, t)}
          onSelect={(r, c) => {
            const metric = grid.metric(r, c);
            const d = defs.get(metric);
            if (d) setCell({ metric, coords: writeCoords(d.dimensions, grid, r, c, filters) });
          }}
        />
      )}
      {cell && <CellComments {...cell} />}
    </div>
  );
}

function Body(props: {
  grid: Grid;
  names: Names;
  display: Display;
  editable: (metric: string) => boolean;
  onWrite?: (r: string[], c: string[], text: string) => Promise<boolean | null>;
  onSelect?: (r: string[], c: string[]) => void;
}) {
  const g = props.grid;
  const names = props.names;
  if (props.display === Display.KPI)
    return <p className="text-4xl font-bold">{total(g).toLocaleString("ja-JP")}</p>;
  if (props.display === Display.LINE || props.display === Display.BAR) {
    const { series, data } = chartRows(g, names);
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
              <th key={d}>{label(names, d)}</th>
            ))}
            {g.colKeys.map((c) => (
              <th key={JSON.stringify(c)}>{keyLabel(c, names) || "値"}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {g.rowKeys.map((r) => (
            <tr key={JSON.stringify(r)}>
              {r.map((m, i) => (
                <th key={i} className="text-left">
                  {m ? label(names, m) : "(なし)"}
                </th>
              ))}
              {g.colKeys.map((c) => {
                // A member value is an id: show its name. An edit of a member cell takes a name.
                const text = formatValue(g.value(r, c), names);
                return (
                  <td
                    key={JSON.stringify(c)}
                    className="text-right"
                    onClick={() => props.onSelect?.(r, c)}
                  >
                    {props.editable(g.metric(r, c)) ? (
                      <Input
                        key={text}
                        aria-label={`${keyLabel(r, names)} ${keyLabel(c, names) || "値"}`}
                        className="h-7 w-24 px-1 py-0 text-right"
                        defaultValue={text}
                        onBlur={(e) => {
                          const input = e.target;
                          if (input.value === text) return;
                          // A refused write must not leave the typed value in the grid.
                          void props.onWrite?.(r, c, input.value).then((ok) => {
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
  const { appId, names } = useApp();
  const mutate = useMutate("comments");
  const [body, setBody] = useState("");
  const { data: resp } = useQuery({
    queryKey: ["app", appId, "comments", props.metric],
    queryFn: () => api.listComments({ appId, metric: props.metric }),
  });
  const list = resp?.comments.filter((c) => sameCell(c.cell, props.coords)) ?? [];
  return (
    <div className="rounded border p-2 text-sm">
      <p className="font-bold">
        コメント: {label(names, props.metric)}{" "}
        {Object.entries(props.coords)
          .map(([d, m]) => `${label(names, d)}=${label(names, m)}`)
          .join(", ")}
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
          onPress={() => {
            // One id for the user action: a resend of the write carries the same comment.
            const id = newId();
            return mutate(async (clientOpId) => {
              await api.addComment({
                appId,
                clientOpId,
                comment: { id, metric: props.metric, cell: props.coords, body },
              });
              setBody("");
            });
          }}
        >
          追加
        </Button>
      </div>
    </div>
  );
}
