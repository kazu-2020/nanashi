import type { MessageInitShape } from "@bufbuild/protobuf";
import { Button, Input, TextArea } from "@heroui/react";
import { useState } from "react";
import { FileTrigger } from "react-aria-components";
import { api } from "../api";
import { ListKind, type MemberEditSchema, PropertyType, Role } from "../gen/nanashi/v1/plan_pb";
import { parseCsv } from "../logic";
import { cls, memberNames, useApp, useMutate } from "../state";
import { Check, Section, Sel } from "../ui";

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
                    <Input
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
                            ariaLabel={p.name}
                            value={v}
                            onChange={set}
                            empty=""
                            options={memberNames(model, p.target)}
                          />
                        ) : (
                          <Input
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
                      {(
                        [
                          [-1, "↑", i === 0],
                          [1, "↓", i === list.members.length - 1],
                        ] as const
                      ).map(([step, arrow, end]) => (
                        <Button
                          key={arrow}
                          size="sm"
                          variant="ghost"
                          isDisabled={end}
                          onPress={() =>
                            edit([
                              {
                                edit: { case: "move", value: { name: m.name, position: i + step } },
                              },
                            ])
                          }
                        >
                          {arrow}
                        </Button>
                      ))}
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

export function ImportPage() {
  const [csv, setCsv] = useState("");
  const [to, setTo] = useState("metric");
  const header = parseCsv(csv)[0] ?? [];
  return (
    <Section title="インポート">
      <FileTrigger
        acceptedFileTypes={[".csv", "text/csv"]}
        onSelect={async (files) => setCsv((await files?.[0]?.text()) ?? "")}
      >
        <Button variant="secondary" className="self-start">
          CSV ファイルを選択
        </Button>
      </FileTrigger>
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
          onChange={setTo}
          options={[
            ["metric", "メトリック"],
            ["list", "リスト"],
          ]}
        />
      </div>
      {to === "list" ? (
        <ImportListForm csv={csv} header={header} />
      ) : (
        <ImportMetricForm csv={csv} header={header} />
      )}
    </Section>
  );
}

const colSel = (header: string[], label: string, value: string, onChange: (v: string) => void) => (
  <Sel key={label} label={label} value={value} onChange={onChange} empty="" options={header} />
);

const mappedColumns = (map: Record<string, string>, fields: string[]) =>
  Object.fromEntries(Object.entries(map).filter(([k, v]) => v && fields.includes(k)));

function ImportListForm(props: { csv: string; header: string[] }) {
  const { appId, model } = useApp();
  const mutate = useMutate();
  const [target, setTarget] = useState("");
  const [memberColumn, setMemberColumn] = useState("");
  const [map, setMap] = useState<Record<string, string>>({});
  const [result, setResult] = useState("");
  const fields = model.lists.find((l) => l.name === target)?.properties.map((p) => p.name) ?? [];
  return (
    <>
      <div className="flex flex-wrap gap-2">
        <Sel
          label="リスト"
          value={target}
          onChange={setTarget}
          empty=""
          options={model.lists.map((l) => l.name)}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {colSel(props.header, "メンバーの列", memberColumn, setMemberColumn)}
        {fields.map((f) =>
          colSel(props.header, `${f} の列`, map[f] ?? "", (v) => setMap({ ...map, [f]: v })),
        )}
      </div>
      <Button
        onPress={() =>
          mutate(async () => {
            const r = await api.import({
              appId,
              csv: props.csv,
              target: {
                case: "list",
                value: { list: target, memberColumn, propertyColumns: mappedColumns(map, fields) },
              },
            });
            setResult(`${r.rows} 行を読み込みました。`);
          })
        }
      >
        インポート
      </Button>
      {result && <p className="text-sm">{result}</p>}
    </>
  );
}

function ImportMetricForm(props: { csv: string; header: string[] }) {
  const { appId, model } = useApp();
  const mutate = useMutate();
  const [target, setTarget] = useState("");
  const [map, setMap] = useState<Record<string, string>>({});
  const [valueColumn, setValueColumn] = useState("");
  const [addMembers, setAddMembers] = useState(true);
  const [result, setResult] = useState("");
  const fields = model.metrics.find((m) => m.name === target)?.dimensions ?? [];
  return (
    <>
      <div className="flex flex-wrap gap-2">
        <Sel
          label="メトリック"
          value={target}
          onChange={setTarget}
          empty=""
          options={model.metrics.map((m) => m.name)}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {fields.map((f) =>
          colSel(props.header, `${f} の列`, map[f] ?? "", (v) => setMap({ ...map, [f]: v })),
        )}
        {colSel(props.header, "値の列", valueColumn, setValueColumn)}
        <Check isSelected={addMembers} onChange={setAddMembers}>
          無いメンバーを追加
        </Check>
      </div>
      <Button
        onPress={() =>
          mutate(async () => {
            const r = await api.import({
              appId,
              csv: props.csv,
              target: {
                case: "metric",
                value: {
                  metric: target,
                  dimensionColumns: mappedColumns(map, fields),
                  valueColumn,
                  addMembers,
                },
              },
            });
            setResult(`${r.rows} 行を読み込みました。`);
          })
        }
      >
        インポート
      </Button>
      {result && <p className="text-sm">{result}</p>}
    </>
  );
}
