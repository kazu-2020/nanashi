import { useEffect, useState, type FormEvent } from "react";
import {
  Alert,
  Button,
  Input,
  Label,
  ListBox,
  Select,
  Spinner,
  Switch,
  TextArea,
  TextField,
  tableVariants,
} from "@heroui/react";
import { ConnectError } from "@connectrpc/connect";
import type { MessageInitShape } from "@bufbuild/protobuf";
import type { Dimension, DimensionOpSchema } from "./gen/nanashi/v1/dimension_pb";
import { dimensionClient } from "./client";

type Op = MessageInitShape<typeof DimensionOpSchema>["op"];

// `loads` counts the reads. The table remounts on each read, so the inputs show the stored values again.
type Data = { seq: bigint; dimensions: Dimension[]; loads: number };

// Calculations.

function withValue(
  values: { [member: string]: string },
  member: string,
  target: string | null,
): { [member: string]: string } {
  const next = { ...values };
  if (target === null) delete next[member];
  else next[member] = target;
  return next;
}

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter((s) => s !== "");
}

function text(f: FormData, name: string): string {
  const v = f.get(name);
  return typeof v === "string" ? v.trim() : "";
}

function messageOf(e: unknown): string {
  return ConnectError.from(e).rawMessage;
}

const t = tableVariants();

// Actions.

export default function Dimensions({ modelId }: { modelId: string }) {
  const [data, setData] = useState<Data>();
  const [selected, setSelected] = useState<string>();
  const [error, setError] = useState<string>();

  async function load() {
    try {
      const r = await dimensionClient.listDimensions({ modelId });
      setData((d) => ({ seq: r.seq, dimensions: r.dimensions, loads: (d?.loads ?? 0) + 1 }));
    } catch (e) {
      setError(messageOf(e));
    }
  }

  useEffect(() => {
    dimensionClient.listDimensions({ modelId }).then(
      (r) => setData({ seq: r.seq, dimensions: r.dimensions, loads: 1 }),
      (e: unknown) => setError(messageOf(e)),
    );
  }, [modelId]);

  async function write(op: Op): Promise<boolean> {
    setError(undefined);
    try {
      await dimensionClient.writeDimensions({
        modelId,
        clientOpId: crypto.randomUUID(),
        expect: data?.seq,
        reason: "画面から",
        ops: [{ op }],
      });
      return true;
    } catch (e) {
      setError(messageOf(e));
      return false;
    } finally {
      await load();
    }
  }

  if (!data) return error ? <ErrorAlert message={error} /> : <Spinner />;

  const dim = data.dimensions.find((d) => d.name === selected) ?? data.dimensions[0];
  const targets = (name: string) => data.dimensions.find((d) => d.name === name)?.members ?? [];

  async function addDimension(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const f = new FormData(form);
    const name = text(f, "name");
    const ok = await write({
      case: "addDimension",
      value: { name, members: lines(text(f, "members")), ordered: f.get("ordered") !== null },
    });
    if (ok) {
      form.reset();
      setSelected(name);
    }
  }

  async function addProperty(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!dim) return;
    const form = e.currentTarget;
    const f = new FormData(form);
    const ok = await write({
      case: "addProperty",
      value: {
        dimension: dim.name,
        name: text(f, "name"),
        targetDimension: text(f, "target"),
        values: {},
      },
    });
    if (ok) form.reset();
  }

  return (
    <div className="flex flex-col gap-4">
      {error && <ErrorAlert message={error} />}
      <div className="flex gap-6">
        <aside className="flex w-56 shrink-0 flex-col gap-4">
          {data.dimensions.length > 0 && (
            <ListBox
              aria-label="軸"
              selectionMode="single"
              disallowEmptySelection
              selectedKeys={dim ? [dim.name] : []}
              onSelectionChange={(keys) => {
                if (keys !== "all") setSelected(String([...keys][0]));
              }}
            >
              {data.dimensions.map((d) => (
                <ListBox.Item key={d.name} id={d.name} textValue={d.name}>
                  {d.name}
                  <ListBox.ItemIndicator />
                </ListBox.Item>
              ))}
            </ListBox>
          )}
          <form className="flex flex-col gap-2" onSubmit={addDimension}>
            <TextField name="name" isRequired>
              <Label>軸の名前</Label>
              <Input />
            </TextField>
            <Switch name="ordered">
              <Switch.Content>
                <Switch.Control>
                  <Switch.Thumb />
                </Switch.Control>
                順序付き
              </Switch.Content>
            </Switch>
            <TextField name="members">
              <Label>項目（1 行に 1 つ）</Label>
              <TextArea rows={4} />
            </TextField>
            <Button type="submit">軸を追加</Button>
          </form>
        </aside>

        {dim && (
          <section key={data.loads} className="flex min-w-0 flex-1 flex-col gap-4">
            <div className={t.base()}>
              <div className={t.scrollContainer()}>
                <table className={t.content()}>
                  <thead className={t.header()}>
                    <tr>
                      <th className={t.column()}>名前</th>
                      {dim.properties.map((p) => (
                        <th key={p.name} className={t.column()}>
                          {p.name}
                        </th>
                      ))}
                      <th className={t.column()} />
                    </tr>
                  </thead>
                  <tbody className={t.body()}>
                    {dim.members.map((m) => (
                      <tr key={m.id.toString()} className={t.row()}>
                        <td className={t.cell()}>
                          <TextField aria-label="名前" defaultValue={m.name}>
                            <Input
                              onKeyDown={(e) => {
                                if (e.key === "Enter") e.currentTarget.blur();
                              }}
                              onBlur={(e) => {
                                const name = e.currentTarget.value.trim();
                                if (name !== "" && name !== m.name)
                                  void write({
                                    case: "renameMember",
                                    value: { dimension: dim.name, oldName: m.name, newName: name },
                                  });
                              }}
                            />
                          </TextField>
                        </td>
                        {dim.properties.map((p) => (
                          <td key={p.name} className={t.cell()}>
                            <Select
                              aria-label={p.name}
                              placeholder="—"
                              value={p.values[m.name] ?? null}
                              onChange={(key) =>
                                void write({
                                  case: "addProperty",
                                  value: {
                                    dimension: dim.name,
                                    name: p.name,
                                    targetDimension: p.targetDimension,
                                    values: withValue(
                                      p.values,
                                      m.name,
                                      key === null ? null : String(key),
                                    ),
                                  },
                                })
                              }
                            >
                              <Select.Trigger>
                                <Select.Value />
                                <Select.ClearButton />
                                <Select.Indicator />
                              </Select.Trigger>
                              <Select.Popover>
                                <ListBox>
                                  {targets(p.targetDimension).map((tm) => (
                                    <ListBox.Item key={tm.name} id={tm.name} textValue={tm.name}>
                                      {tm.name}
                                      <ListBox.ItemIndicator />
                                    </ListBox.Item>
                                  ))}
                                </ListBox>
                              </Select.Popover>
                            </Select>
                          </td>
                        ))}
                        <td className={t.cell()}>
                          <Button
                            size="sm"
                            variant="danger-soft"
                            onPress={() =>
                              void write({
                                case: "removeMember",
                                value: { dimension: dim.name, name: m.name },
                              })
                            }
                          >
                            削除
                          </Button>
                        </td>
                      </tr>
                    ))}
                    <tr className={t.row()}>
                      <td className={t.cell()} colSpan={dim.properties.length + 2}>
                        <TextField aria-label="項目を追加">
                          <Input
                            placeholder="+ 項目を追加"
                            onKeyDown={(e) => {
                              const name = e.currentTarget.value.trim();
                              if (e.key === "Enter" && name !== "")
                                void write({
                                  case: "addMember",
                                  value: { dimension: dim.name, name },
                                });
                            }}
                          />
                        </TextField>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <form className="flex items-end gap-2" onSubmit={addProperty}>
              <TextField name="name" isRequired>
                <Label>プロパティの名前</Label>
                <Input />
              </TextField>
              <Select name="target" isRequired placeholder="選択" className="w-48">
                <Label>対象の軸</Label>
                <Select.Trigger>
                  <Select.Value />
                  <Select.Indicator />
                </Select.Trigger>
                <Select.Popover>
                  <ListBox>
                    {data.dimensions
                      .filter((d) => d.name !== dim.name)
                      .map((d) => (
                        <ListBox.Item key={d.name} id={d.name} textValue={d.name}>
                          {d.name}
                          <ListBox.ItemIndicator />
                        </ListBox.Item>
                      ))}
                  </ListBox>
                </Select.Popover>
              </Select>
              <Button type="submit">プロパティを追加</Button>
            </form>
          </section>
        )}
      </div>
    </div>
  );
}

function ErrorAlert({ message }: { message: string }) {
  return (
    <Alert status="danger" role="alert">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>{message}</Alert.Title>
      </Alert.Content>
    </Alert>
  );
}
