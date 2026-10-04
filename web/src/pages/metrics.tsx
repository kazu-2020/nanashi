import { Button, Input, TextArea } from "@heroui/react";
import { useState } from "react";
import { Code, ConnectError } from "@connectrpc/connect";
import { api, errorText } from "../api";
import { ItemType, type MetricDef, Role, ValueKind } from "../gen/nanashi/v1/plan_pb";
import { defaultSpec, toFilters } from "../logic";
import { PivotEditor } from "../Pivot";
import { cls, useApp, useMutate } from "../state";
import { Check, Checks, Sel } from "../ui";

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
    kind: d?.kind ?? ValueKind.NUMBER,
    memberList: d?.memberList ?? "",
    formula: d?.formula ?? "",
    overridable: d?.overridable ?? false,
  });
  const [newName, setNewName] = useState("");
  const kinds = [
    { kind: ValueKind.NUMBER, memberList: "", label: "数値" },
    { kind: ValueKind.BOOLEAN, memberList: "", label: "真偽値" },
    ...model.lists.map((l) => ({
      kind: ValueKind.MEMBER,
      memberList: l.name,
      label: `メンバー: ${l.name}`,
    })),
  ];
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
          value={String(kinds.findIndex((k) => k.kind === f.kind && k.memberList === f.memberList))}
          onChange={(i) => {
            const { kind, memberList } = kinds[Number(i)];
            setF({ ...f, kind, memberList });
          }}
          options={kinds.map((k, i): [string, string] => [String(i), k.label])}
        />
        <Check isSelected={f.overridable} onChange={(overridable) => setF({ ...f, overridable })}>
          上書き入力を許可
        </Check>
      </div>
      <div className="text-sm">
        ディメンション:{" "}
        <Checks
          label="ディメンション"
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
            <Button
              size="sm"
              variant={m.name === sel ? "primary" : "ghost"}
              onPress={() => setSel(m.name)}
            >
              {m.name}
              {m.formula ? " ƒ" : ""}
            </Button>
          </li>
        ))}
      </ul>
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        {can(Role.MODELER) && (
          <MetricEditor key={JSON.stringify(def ?? sel)} def={def} onSaved={setSel} />
        )}
        {def && (
          <PivotEditor
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
            label="メトリック"
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
        <PivotEditor key={t.id + t.metrics.join()} initial={defaultSpec(t.metrics, dims)} />
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
        <PivotEditor
          key={v.id}
          view={{ id: v.id, name: v.name }}
          initial={{ ...v, filters: toFilters(v.filters) }}
        />
      )}
    </div>
  );
}
