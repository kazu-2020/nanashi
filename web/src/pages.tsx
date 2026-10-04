// The screens of an application. Each screen reads the model from AppCtx.
import type { MessageInitShape } from "@bufbuild/protobuf";
import { Button, Input, TextArea } from "@heroui/react";
import { useState } from "react";
import { Code, ConnectError } from "@connectrpc/connect";
import { api, errorText } from "./api";
import {
  ItemType,
  ListKind,
  type MemberEditSchema,
  type MetricDef,
  type WidgetSchema,
  PropertyType,
  Role,
} from "./gen/nanashi/v1/plan_pb";
import { defaultSpec, parseCsv, ROLES, toFilters } from "./logic";
import { Pivot } from "./Pivot";
import { cls, time, useApp, useLoad, useRun } from "./state";
import { Checks, Section, Sel } from "./ui";

const KINDS: [string, string][] = [
  [String(ListKind.DIMENSION), "ディメンション"],
  [String(ListKind.TRANSACTION), "トランザクション"],
];
const PROP_TYPES: [string, string][] = [
  [String(PropertyType.DIMENSION), "ディメンション"],
  [String(PropertyType.NUMBER), "数値"],
  [String(PropertyType.BOOLEAN), "真偽値"],
  [String(PropertyType.TEXT), "テキスト"],
];
const lines = (s: string) =>
  s
    .split(/\r?\n/)
    .map((x) => x.trim())
    .filter(Boolean);

// useMutate runs an action, then reads the model again.
function useMutate() {
  const run = useRun();
  const { reload } = useApp();
  return (f: () => Promise<unknown>) =>
    run(async () => {
      await f();
      await reload();
    });
}

export function ListsPage() {
  const { appId, model, can } = useApp();
  const mutate = useMutate();
  const [sel, setSel] = useState(model.lists[0]?.name ?? "");
  const [form, setForm] = useState({ name: "", kind: String(ListKind.DIMENSION), members: "" });
  const [prop, setProp] = useState({ name: "", type: String(PropertyType.NUMBER), target: "" });
  const [newMember, setNewMember] = useState("");
  const list = model.lists.find((l) => l.name === sel);
  const modeler = can(Role.MODELER);
  const edit = (edits: MessageInitShape<typeof MemberEditSchema>[]) =>
    mutate(() => api.editMembers({ appId, list: sel, edits }));

  return (
    <div>
      {modeler && (
        <Section title="リストを作成">
          <div className="flex flex-wrap items-start gap-2">
            <Input
              aria-label="リスト名"
              placeholder="リスト名"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <Sel
              label="種類"
              value={form.kind}
              onChange={(kind) => setForm({ ...form, kind })}
              options={KINDS}
            />
            <TextArea
              aria-label="メンバー"
              placeholder="メンバー（1 行に 1 つ）"
              value={form.members}
              onChange={(e) => setForm({ ...form, members: e.target.value })}
            />
            <Button
              onPress={() =>
                mutate(async () => {
                  await api.createList({
                    appId,
                    name: form.name,
                    kind: Number(form.kind),
                    members: lines(form.members),
                  });
                  setSel(form.name);
                  setForm({ ...form, name: "", members: "" });
                })
              }
            >
              作成
            </Button>
          </div>
        </Section>
      )}
      <Section title="リスト">
        <Sel
          label="リスト"
          value={sel}
          onChange={setSel}
          options={model.lists.map((l) => l.name)}
        />
      </Section>
      {list && (
        <Section title={`${list.name} のメンバー（${list.members.length}）`}>
          <table className={cls.table}>
            <thead>
              <tr>
                <th>名前</th>
                {list.properties.map((p) => (
                  <th key={p.name}>
                    {p.name}
                    {p.target && ` → ${p.target}`}
                  </th>
                ))}
                {modeler && <th>操作</th>}
              </tr>
            </thead>
            <tbody>
              {list.members.map((m, i) => (
                <tr key={m.name}>
                  <td>
                    <input
                      key={m.name}
                      aria-label="メンバー名"
                      disabled={!modeler}
                      defaultValue={m.name}
                      onBlur={(e) =>
                        e.target.value !== m.name &&
                        edit([
                          {
                            edit: {
                              case: "rename",
                              value: { name: m.name, newName: e.target.value },
                            },
                          },
                        ])
                      }
                    />
                  </td>
                  {list.properties.map((p) => {
                    const v = m.properties[p.name] ?? "";
                    const set = (value: string) =>
                      value !== v &&
                      edit([
                        {
                          edit: {
                            case: "set",
                            value: { name: m.name, properties: { [p.name]: value } },
                          },
                        },
                      ]);
                    return (
                      <td key={p.name}>
                        {p.type === PropertyType.DIMENSION ? (
                          <Sel
                            value={v}
                            onChange={set}
                            empty=""
                            options={
                              model.lists
                                .find((l) => l.name === p.target)
                                ?.members.map((t) => t.name) ?? []
                            }
                          />
                        ) : (
                          <input
                            key={v}
                            aria-label={p.name}
                            disabled={!modeler}
                            defaultValue={v}
                            onBlur={(e) => set(e.target.value)}
                          />
                        )}
                      </td>
                    );
                  })}
                  {modeler && (
                    <td className="whitespace-nowrap">
                      <Button
                        size="sm"
                        variant="ghost"
                        isDisabled={i === 0}
                        onPress={() =>
                          edit([
                            { edit: { case: "move", value: { name: m.name, position: i - 1 } } },
                          ])
                        }
                      >
                        ↑
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        isDisabled={i === list.members.length - 1}
                        onPress={() =>
                          edit([
                            { edit: { case: "move", value: { name: m.name, position: i + 1 } } },
                          ])
                        }
                      >
                        ↓
                      </Button>
                      <Button
                        size="sm"
                        variant="danger-soft"
                        onPress={() =>
                          edit([{ edit: { case: "remove", value: { name: m.name } } }])
                        }
                      >
                        削除
                      </Button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {modeler && (
            <>
              <div className="flex gap-2">
                <Input
                  aria-label="新しいメンバー"
                  placeholder="新しいメンバー"
                  value={newMember}
                  onChange={(e) => setNewMember(e.target.value)}
                />
                <Button
                  onPress={() =>
                    edit([{ edit: { case: "add", value: { name: newMember } } }]).then(
                      (ok) => ok && setNewMember(""),
                    )
                  }
                >
                  メンバーを追加
                </Button>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label="プロパティ名"
                  placeholder="プロパティ名"
                  value={prop.name}
                  onChange={(e) => setProp({ ...prop, name: e.target.value })}
                />
                <Sel
                  label="型"
                  value={prop.type}
                  onChange={(type) => setProp({ ...prop, type })}
                  options={PROP_TYPES}
                />
                {prop.type === String(PropertyType.DIMENSION) && (
                  <Sel
                    label="対象リスト"
                    value={prop.target}
                    onChange={(target) => setProp({ ...prop, target })}
                    empty=""
                    options={model.lists.map((l) => l.name)}
                  />
                )}
                <Button
                  onPress={() =>
                    mutate(async () => {
                      await api.addProperty({
                        appId,
                        list: sel,
                        property: { name: prop.name, type: Number(prop.type), target: prop.target },
                      });
                      setProp({ ...prop, name: "" });
                    })
                  }
                >
                  プロパティを追加
                </Button>
              </div>
            </>
          )}
        </Section>
      )}
    </div>
  );
}

export function CalendarPage() {
  const { appId, model, can } = useApp();
  const mutate = useMutate();
  const [start, setStart] = useState(String(new Date().getFullYear()));
  const [years, setYears] = useState("2");
  const cal = model.lists.filter((l) => l.kind === ListKind.CALENDAR);
  return (
    <Section title="カレンダー">
      <p className="text-sm">
        {cal.length
          ? cal.map((l) => `${l.name}（${l.members.length}）`).join("、")
          : "カレンダーはまだありません。"}
      </p>
      {can(Role.MODELER) && (
        <div className="flex gap-2">
          <Input
            aria-label="開始年"
            type="number"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
          <Input
            aria-label="年数"
            type="number"
            value={years}
            onChange={(e) => setYears(e.target.value)}
          />
          <Button
            onPress={() =>
              mutate(() =>
                api.createCalendar({ appId, startYear: Number(start), years: Number(years) }),
              )
            }
          >
            カレンダーを作成
          </Button>
        </div>
      )}
    </Section>
  );
}

export function ScenariosPage() {
  const { appId, model, can } = useApp();
  const mutate = useMutate();
  const [name, setName] = useState("");
  const [from, setFrom] = useState("");
  const members = model.lists.find((l) => l.kind === ListKind.SCENARIO)?.members ?? [];
  return (
    <Section title="シナリオ">
      <ul className="list-disc pl-6 text-sm">
        {members.map((m) => (
          <li key={m.name}>{m.name}</li>
        ))}
      </ul>
      <p className="text-sm">
        シナリオを比較するには、メトリックのディメンションに Scenario を入れて、Scenario
        を列に置きます。
      </p>
      {can(Role.MODELER) && (
        <div className="flex gap-2">
          <Input
            aria-label="シナリオ名"
            placeholder="シナリオ名"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Sel
            label="コピー元"
            value={from}
            onChange={setFrom}
            empty="（空）"
            options={members.map((m) => m.name)}
          />
          <Button onPress={() => mutate(() => api.createScenario({ appId, name, copyFrom: from }))}>
            シナリオを作成
          </Button>
        </div>
      )}
    </Section>
  );
}

const FORMULA_HELP: [string, string][] = [
  ["演算子", "+ - * /、比較 = <> < <= > >=、論理 AND OR NOT"],
  [
    "集計",
    "X[BY SUM: Employee.Department]、X[REMOVE SUM: Product, Month]（SUM AVG MIN MAX COUNT）",
  ],
  ["BY での割り当て", "Rate[BY: Employee.Department]"],
  ["スライスと絞り込み", 'X[SELECT: Version."実績"]、X[SELECT: Month - 1]、X[FILTER: 条件]'],
  ["ディメンションの追加", "X[EXPAND: Month]、X[ON: Y]"],
  ["関数", "IF(条件, a[, b])、IFBLANK(x, 定数)、ISBLANK(x)、PREVIOUS(Month[, n])"],
  ["メンバー", 'Month（各セルのメンバー）、Month."Mar"（定数）'],
  ["名前", "Revenue、売上、'Unit Price'（空白を含む名前）"],
  ["トランザクション", "'Sales.Amount'[BY SUM: Sales.Product]"],
];

function MetricEditor(props: { def?: MetricDef; onSaved: (name: string) => void }) {
  const { appId, model } = useApp();
  const mutate = useMutate();
  const d = props.def;
  const [f, setF] = useState({
    name: d?.name ?? "",
    dimensions: d?.dimensions ?? [],
    kind: d?.kind || "number",
    formula: d?.formula ?? "",
    overridable: d?.overridable ?? false,
  });
  const [newName, setNewName] = useState("");
  return (
    <div className="flex flex-col gap-2 rounded border p-2">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          aria-label="メトリック名"
          placeholder="メトリック名"
          disabled={!!d}
          value={f.name}
          onChange={(e) => setF({ ...f, name: e.target.value })}
        />
        <Sel
          label="値の種類"
          value={f.kind}
          onChange={(kind) => setF({ ...f, kind })}
          options={[
            ["number", "数値"],
            ["boolean", "真偽値"],
            ...model.lists.map((l): [string, string] => [
              `member:${l.name}`,
              `メンバー: ${l.name}`,
            ]),
          ]}
        />
        <label className="inline-flex items-center gap-1 text-sm">
          <input
            type="checkbox"
            checked={f.overridable}
            onChange={(e) => setF({ ...f, overridable: e.target.checked })}
          />
          上書き入力を許可
        </label>
      </div>
      <div className="text-sm">
        ディメンション:{" "}
        <Checks
          options={model.lists.map((l) => l.name)}
          value={f.dimensions}
          onChange={(dimensions) => setF({ ...f, dimensions })}
        />
      </div>
      <TextArea
        aria-label="数式"
        placeholder="数式（空なら入力メトリック）"
        className="font-mono"
        value={f.formula}
        onChange={(e) => setF({ ...f, formula: e.target.value })}
      />
      <details className="text-sm">
        <summary className="cursor-pointer">数式のヘルプ</summary>
        <table className={cls.table}>
          <tbody>
            {FORMULA_HELP.map(([k, v]) => (
              <tr key={k}>
                <th className="text-left">{k}</th>
                <td className="font-mono">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p>
          空白のセルは 0 と異なります。+ と - は片方に値があるセル、* と /
          は両方に値があるセルに値を作ります。
        </p>
      </details>
      <div className="flex flex-wrap gap-2">
        <Button
          onPress={() =>
            mutate(async () => {
              const save = (replace: boolean) => api.saveMetric({ appId, metric: f, replace });
              // The api refuses a change that deletes the input values, until the user accepts it.
              await save(false).catch(async (e: unknown) => {
                const refused = e instanceof ConnectError && e.code === Code.FailedPrecondition;
                if (!refused || !confirm(`${errorText(e)}。変更しますか？`)) throw e;
                await save(true);
              });
              props.onSaved(f.name);
            })
          }
        >
          保存
        </Button>
        {d && (
          <>
            <Input
              aria-label="新しい名前"
              placeholder="新しい名前"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
            <Button
              variant="secondary"
              onPress={() =>
                mutate(async () => {
                  await api.renameMetric({ appId, name: d.name, newName });
                  props.onSaved(newName);
                })
              }
            >
              名前を変更
            </Button>
            <Button
              variant="danger"
              onPress={() =>
                confirm(`${d.name} を削除しますか？`) &&
                mutate(async () => {
                  await api.deleteMetric({ appId, name: d.name });
                  props.onSaved("");
                })
              }
            >
              削除
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

export function MetricsPage() {
  const { model, can } = useApp();
  // "" shows the form for a new Metric.
  const [sel, setSel] = useState(model.metrics[0]?.name ?? "");
  const def = model.metrics.find((m) => m.name === sel);
  return (
    <div className="flex gap-4">
      <ul className="flex w-48 shrink-0 flex-col gap-1 text-sm">
        {can(Role.MODELER) && (
          <li>
            <Button size="sm" variant="secondary" onPress={() => setSel("")}>
              新しいメトリック
            </Button>
          </li>
        )}
        {model.metrics.map((m) => (
          <li key={m.name}>
            <button className={m.name === sel ? "font-bold" : ""} onClick={() => setSel(m.name)}>
              {m.name}
              {m.formula ? " ƒ" : ""}
            </button>
          </li>
        ))}
      </ul>
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        {can(Role.MODELER) && (
          <MetricEditor key={JSON.stringify(def ?? sel)} def={def} onSaved={setSel} />
        )}
        {def && (
          <Pivot
            key={`${def.name}:${def.dimensions.join()}`}
            initial={defaultSpec([def.name], def.dimensions)}
          />
        )}
      </div>
    </div>
  );
}

export function TablesPage() {
  const { appId, model, can } = useApp();
  const mutate = useMutate();
  const [sel, setSel] = useState(model.tables[0]?.id ?? "");
  const t = model.tables.find((x) => x.id === sel);
  const [f, setF] = useState({ name: t?.name ?? "", metrics: t?.metrics ?? [] });
  const open = (id: string) => {
    const x = model.tables.find((y) => y.id === id);
    setSel(id);
    setF({ name: x?.name ?? "", metrics: x?.metrics ?? [] });
  };
  const dims = [
    ...new Set(
      (t?.metrics ?? []).flatMap((m) => model.metrics.find((d) => d.name === m)?.dimensions ?? []),
    ),
  ];
  return (
    <div className="flex flex-col gap-4">
      <Sel
        label="テーブル"
        value={sel}
        onChange={open}
        empty="（新しいテーブル）"
        options={model.tables.map((x): [string, string] => [x.id, x.name])}
      />
      {can(Role.MODELER) && (
        <div className="flex flex-col gap-2 rounded border p-2">
          <Input
            aria-label="テーブル名"
            placeholder="テーブル名"
            value={f.name}
            onChange={(e) => setF({ ...f, name: e.target.value })}
          />
          <Checks
            options={model.metrics.map((m) => m.name)}
            value={f.metrics}
            onChange={(metrics) => setF({ ...f, metrics })}
          />
          <div className="flex gap-2">
            <Button
              onPress={() =>
                mutate(async () => setSel((await api.saveTable({ appId, id: sel, ...f })).id))
              }
            >
              保存
            </Button>
            {t && (
              <Button
                variant="danger"
                onPress={() =>
                  mutate(async () => {
                    await api.deleteItem({ appId, type: ItemType.TABLE, id: t.id });
                    open("");
                  })
                }
              >
                削除
              </Button>
            )}
          </div>
        </div>
      )}
      {t && t.metrics.length > 0 && (
        <Pivot key={t.id + t.metrics.join()} initial={defaultSpec(t.metrics, dims)} />
      )}
    </div>
  );
}

export function ViewsPage() {
  const { appId, model, can } = useApp();
  const mutate = useMutate();
  const [sel, setSel] = useState(model.views[0]?.id ?? "");
  const v = model.views.find((x) => x.id === sel);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-2">
        <Sel
          label="ビュー"
          value={sel}
          onChange={setSel}
          empty=""
          options={model.views.map((x): [string, string] => [x.id, x.name])}
        />
        {v && can(Role.MODELER) && (
          <Button
            variant="danger"
            size="sm"
            onPress={() => mutate(() => api.deleteItem({ appId, type: ItemType.VIEW, id: v.id }))}
          >
            削除
          </Button>
        )}
      </div>
      <p className="text-sm">
        ビューはメトリックやテーブルの画面で「ビューとして保存」すると作れます。
      </p>
      {v && (
        <Pivot
          key={v.id}
          view={{ id: v.id, name: v.name }}
          initial={{ ...v, filters: toFilters(v.filters) }}
        />
      )}
    </div>
  );
}

export function BoardsPage() {
  const { appId, model, can } = useApp();
  const mutate = useMutate();
  const [sel, setSel] = useState(model.boards[0]?.id ?? "");
  const [name, setName] = useState("");
  const [page, setPage] = useState<Record<string, string>>({});
  const [text, setText] = useState("");
  const b = model.boards.find((x) => x.id === sel);
  const modeler = can(Role.MODELER);
  const save = (patch: {
    widgets?: MessageInitShape<typeof WidgetSchema>[];
    pageSelectors?: string[];
  }) =>
    b &&
    mutate(() =>
      api.saveBoard({
        appId,
        id: b.id,
        name: b.name,
        widgets: b.widgets,
        pageSelectors: b.pageSelectors,
        ...patch,
      }),
    );
  const order = (d: string) =>
    model.lists.find((l) => l.name === d)?.members.map((m) => m.name) ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <Sel
          label="ボード"
          value={sel}
          onChange={setSel}
          empty=""
          options={model.boards.map((x): [string, string] => [x.id, x.name])}
        />
        {modeler && (
          <>
            <Input
              aria-label="ボード名"
              placeholder="ボード名"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <Button
              size="sm"
              onPress={() => mutate(async () => setSel((await api.saveBoard({ appId, name })).id))}
            >
              ボードを作成
            </Button>
          </>
        )}
      </div>
      {b && (
        <>
          <div className="flex flex-wrap items-center gap-2 rounded border p-2">
            {b.pageSelectors.map((d) => (
              <Sel
                key={d}
                label={d}
                value={page[d] ?? ""}
                onChange={(m) => setPage({ ...page, [d]: m })}
                empty="すべて"
                options={order(d)}
              />
            ))}
            {modeler && (
              <Sel
                label="ページセレクターを追加"
                value=""
                onChange={(d) => d && save({ pageSelectors: [...b.pageSelectors, d] })}
                empty=""
                options={model.lists.map((l) => l.name).filter((n) => !b.pageSelectors.includes(n))}
              />
            )}
          </div>
          {b.widgets.map((w, i) => {
            const v =
              w.content.case === "viewId"
                ? model.views.find((x) => x.id === w.content.value)
                : undefined;
            return (
              <div key={i} className="rounded border p-2">
                <div className="flex items-center justify-between">
                  <b>{v?.name ?? ""}</b>
                  {modeler && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onPress={() => save({ widgets: b.widgets.filter((_, j) => j !== i) })}
                    >
                      ウィジェットを削除
                    </Button>
                  )}
                </div>
                {w.content.case === "text" && (
                  <p className="whitespace-pre-wrap">{w.content.value}</p>
                )}
                {v && (
                  <Pivot compact initial={{ ...v, filters: toFilters(v.filters) }} page={page} />
                )}
              </div>
            );
          })}
          {modeler && (
            <div className="flex flex-wrap items-center gap-2">
              <Sel
                label="ビューのウィジェットを追加"
                value=""
                onChange={(id) =>
                  id &&
                  save({ widgets: [...b.widgets, { content: { case: "viewId", value: id } }] })
                }
                empty=""
                options={model.views.map((x): [string, string] => [x.id, x.name])}
              />
              <Input
                aria-label="テキスト"
                placeholder="テキスト"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <Button
                size="sm"
                onPress={() =>
                  save({ widgets: [...b.widgets, { content: { case: "text", value: text } }] })
                }
              >
                テキストを追加
              </Button>
              <Button
                size="sm"
                variant="danger"
                onPress={() =>
                  mutate(() => api.deleteItem({ appId, type: ItemType.BOARD, id: b.id }))
                }
              >
                ボードを削除
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function ImportPage() {
  const { appId, model } = useApp();
  const mutate = useMutate();
  const [csv, setCsv] = useState("");
  const [to, setTo] = useState("metric");
  const [target, setTarget] = useState("");
  const [memberColumn, setMemberColumn] = useState("");
  const [map, setMap] = useState<Record<string, string>>({});
  const [valueColumn, setValueColumn] = useState("");
  const [addMembers, setAddMembers] = useState(true);
  const [result, setResult] = useState("");
  const header = parseCsv(csv)[0] ?? [];
  const fields =
    to === "list"
      ? (model.lists.find((l) => l.name === target)?.properties.map((p) => p.name) ?? [])
      : (model.metrics.find((m) => m.name === target)?.dimensions ?? []);
  const colSel = (label: string, value: string, onChange: (v: string) => void) => (
    <Sel key={label} label={label} value={value} onChange={onChange} empty="" options={header} />
  );
  const mapped = Object.fromEntries(
    Object.entries(map).filter(([k, v]) => v && fields.includes(k)),
  );
  return (
    <Section title="インポート">
      <input
        type="file"
        accept=".csv,text/csv"
        aria-label="CSV ファイル"
        onChange={async (e) => setCsv((await e.target.files?.[0]?.text()) ?? "")}
      />
      <TextArea
        aria-label="CSV"
        placeholder="CSV を貼り付け（1 行目は見出し）"
        rows={6}
        value={csv}
        onChange={(e) => setCsv(e.target.value)}
      />
      <p className="text-sm">見出し: {header.join(", ")}</p>
      <div className="flex flex-wrap gap-2">
        <Sel
          label="読み込み先"
          value={to}
          onChange={(v) => (setTo(v), setTarget(""), setMap({}))}
          options={[
            ["metric", "メトリック"],
            ["list", "リスト"],
          ]}
        />
        <Sel
          label={to === "list" ? "リスト" : "メトリック"}
          value={target}
          onChange={setTarget}
          empty=""
          options={
            to === "list" ? model.lists.map((l) => l.name) : model.metrics.map((m) => m.name)
          }
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {to === "list" && colSel("メンバーの列", memberColumn, setMemberColumn)}
        {fields.map((f) => colSel(`${f} の列`, map[f] ?? "", (v) => setMap({ ...map, [f]: v })))}
        {to === "metric" && colSel("値の列", valueColumn, setValueColumn)}
        {to === "metric" && (
          <label className="inline-flex items-center gap-1 text-sm">
            <input
              type="checkbox"
              checked={addMembers}
              onChange={(e) => setAddMembers(e.target.checked)}
            />
            無いメンバーを追加
          </label>
        )}
      </div>
      <Button
        onPress={() =>
          mutate(async () => {
            const r = await api.import({
              appId,
              csv,
              target:
                to === "list"
                  ? { case: "list", value: { list: target, memberColumn, propertyColumns: mapped } }
                  : {
                      case: "metric",
                      value: { metric: target, dimensionColumns: mapped, valueColumn, addMembers },
                    },
            });
            setResult(`${r.rows} 行を読み込みました。`);
          })
        }
      >
        インポート
      </Button>
      {result && <p className="text-sm">{result}</p>}
    </Section>
  );
}

export function CommentsPage() {
  const { appId } = useApp();
  const [r] = useLoad(() => api.listComments({ appId }), [appId]);
  return (
    <Section title="コメント">
      <table className={cls.table}>
        <thead>
          <tr>
            <th>日時</th>
            <th>ユーザー</th>
            <th>対象</th>
            <th>セル</th>
            <th>本文</th>
          </tr>
        </thead>
        <tbody>
          {r?.comments.map((c) => (
            <tr key={c.id}>
              <td>{time(c.createdAt)}</td>
              <td>{c.user}</td>
              <td>{c.target}</td>
              <td>
                {Object.entries(c.cell)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(", ")}
              </td>
              <td>{c.body}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

export function AuditPage() {
  const { appId, model } = useApp();
  const [r] = useLoad(() => api.listAudit({ appId, limit: 200 }), [appId, model.seq]);
  return (
    <Section title="監査ログ">
      <table className={cls.table}>
        <thead>
          <tr>
            <th>日時</th>
            <th>ユーザー</th>
            <th>操作</th>
            <th>内容</th>
          </tr>
        </thead>
        <tbody>
          {r?.entries.map((e) => (
            <tr key={e.id}>
              <td className="whitespace-nowrap">{time(e.createdAt)}</td>
              <td>{e.user}</td>
              <td>{e.action}</td>
              <td className="max-w-xl font-mono text-xs break-all">{e.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

export function SnapshotsPage() {
  const { appId, can } = useApp();
  const run = useRun();
  const [name, setName] = useState("");
  const [r, again] = useLoad(() => api.listSnapshots({ appId }), [appId]);
  return (
    <Section title="スナップショット">
      {can(Role.CONTRIBUTOR) && (
        <div className="flex gap-2">
          <Input
            aria-label="スナップショット名"
            placeholder="スナップショット名"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Button
            onPress={() =>
              run(async () => {
                await api.createSnapshot({ appId, name });
                setName("");
                again();
              })
            }
          >
            作成
          </Button>
        </div>
      )}
      <p className="text-sm">
        復元はアプリケーション一覧の「スナップショットから復元」で新しいアプリケーションに行います。
      </p>
      <ul className="list-disc pl-6 text-sm">
        {r?.snapshots.map((s) => (
          <li key={s.id}>
            {s.name}（{s.user}、{time(s.createdAt)}）
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function AccessPage() {
  const { appId, model } = useApp();
  const run = useRun();
  const [r, again] = useLoad(() => api.getAccess({ appId }), [appId]);
  const [user, setUser] = useState("");
  const [role, setRole] = useState(String(Role.VIEWER));
  const [rule, setRule] = useState({
    role: String(Role.VIEWER),
    list: "",
    members: [] as string[],
    write: false,
  });
  const act = (f: () => Promise<unknown>) => run(async () => (await f(), again()));
  const roleName = (x: Role) => ROLES.find(([v]) => v === String(x))?.[1] ?? "";
  return (
    <div>
      <Section title="メンバー">
        <table className={cls.table}>
          <tbody>
            {r?.members.map((m) => (
              <tr key={m.user}>
                <td>{m.user}</td>
                <td>
                  <Sel
                    value={String(m.role)}
                    onChange={(v) =>
                      act(() => api.setMemberRole({ appId, user: m.user, role: Number(v) }))
                    }
                    empty="（外す）"
                    options={ROLES}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex gap-2">
          <Input
            aria-label="ユーザー名"
            placeholder="ユーザー名"
            value={user}
            onChange={(e) => setUser(e.target.value)}
          />
          <Sel value={role} onChange={setRole} options={ROLES} />
          <Button onPress={() => act(() => api.setMemberRole({ appId, user, role: Number(role) }))}>
            追加
          </Button>
        </div>
      </Section>
      <Section title="アクセスルール（閲覧者と入力者に適用）">
        <table className={cls.table}>
          <tbody>
            {r?.rules.map((x) => (
              <tr key={x.id}>
                <td>{roleName(x.role)}</td>
                <td>{x.list}</td>
                <td>{x.members.join(", ")}</td>
                <td>{x.write ? "書き込み可" : "読み取りのみ"}</td>
                <td>
                  <Button
                    size="sm"
                    variant="danger-soft"
                    onPress={() => act(() => api.saveAccessRule({ appId, id: x.id, delete: true }))}
                  >
                    削除
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex flex-wrap items-center gap-2">
          <Sel
            label="ロール"
            value={rule.role}
            onChange={(v) => setRule({ ...rule, role: v })}
            options={ROLES.slice(0, 2)}
          />
          <Sel
            label="リスト"
            value={rule.list}
            onChange={(list) => setRule({ ...rule, list, members: [] })}
            empty=""
            options={model.lists.map((l) => l.name)}
          />
          <Checks
            options={
              model.lists.find((l) => l.name === rule.list)?.members.map((m) => m.name) ?? []
            }
            value={rule.members}
            onChange={(members) => setRule({ ...rule, members })}
          />
          <label className="inline-flex items-center gap-1 text-sm">
            <input
              type="checkbox"
              checked={rule.write}
              onChange={(e) => setRule({ ...rule, write: e.target.checked })}
            />
            書き込み可
          </label>
          <Button
            onPress={() =>
              act(() => api.saveAccessRule({ appId, ...rule, role: Number(rule.role) }))
            }
          >
            ルールを追加
          </Button>
        </div>
      </Section>
    </div>
  );
}
