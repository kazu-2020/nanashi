import { Button, Input } from "@heroui/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, newId } from "../api";
import { Role } from "../gen/nanashi/v1/plan_pb";
import { label, ROLES, roleName } from "../logic";
import { cls, createOrUpdate, listOptions, memberOptions, time, useApp, useMutate } from "../state";
import { Check, Checks, Section, Sel } from "../ui";

export function CommentsPage() {
  const { appId, names } = useApp();
  const { data: r } = useQuery({
    queryKey: ["app", appId, "comments"],
    queryFn: () => api.listComments({ appId }),
  });
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
              <td>{label(names, c.metric)}</td>
              <td>
                {Object.entries(c.cell)
                  .map(([k, v]) => `${label(names, k)}=${label(names, v)}`)
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
  const { appId } = useApp();
  const { data: r } = useQuery({
    queryKey: ["app", appId, "audit"],
    queryFn: () => api.listAudit({ appId, limit: 200 }),
  });
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
  const mutate = useMutate("snapshots");
  const [name, setName] = useState("");
  const [id, setId] = useState(newId);
  const { data: r } = useQuery({
    queryKey: ["app", appId, "snapshots"],
    queryFn: () => api.listSnapshots({ appId }),
  });
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
              mutate(async (clientOpId) => {
                await api.createSnapshot({ appId, clientOpId, id, name });
                setName("");
              }).then((ok) => ok !== null && setId(newId()))
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
  const { appId, model, names } = useApp();
  const mutate = useMutate();
  const { data: r } = useQuery({
    queryKey: ["app", appId, "access"],
    queryFn: () => api.getAccess({ appId }),
  });
  const [user, setUser] = useState("");
  const [role, setRole] = useState(String(Role.VIEWER));
  const [rule, setRule] = useState({
    role: String(Role.VIEWER),
    list: "",
    members: [] as string[],
    write: false,
  });
  const [ruleId, setRuleId] = useState(newId);
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
                    ariaLabel={`${m.user} のロール`}
                    value={String(m.role)}
                    onChange={(v) =>
                      mutate((clientOpId) =>
                        api.setMemberRole({
                          appId,
                          clientOpId,
                          member: { user: m.user, role: Number(v) },
                        }),
                      )
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
          <Sel ariaLabel="追加するロール" value={role} onChange={setRole} options={ROLES} />
          <Button
            onPress={() =>
              mutate((clientOpId) =>
                api.setMemberRole({ appId, clientOpId, member: { user, role: Number(role) } }),
              )
            }
          >
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
                <td>{label(names, x.list)}</td>
                <td>{x.members.map((m) => label(names, m)).join(", ")}</td>
                <td>{x.write ? "書き込み可" : "読み取りのみ"}</td>
                <td>
                  <Button
                    size="sm"
                    variant="danger-soft"
                    onPress={() =>
                      mutate((clientOpId) => api.deleteAccessRule({ appId, clientOpId, id: x.id }))
                    }
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
            options={listOptions(model)}
          />
          <Checks
            label="メンバー"
            options={memberOptions(model, rule.list)}
            value={rule.members}
            onChange={(members) => setRule({ ...rule, members })}
          />
          <Check isSelected={rule.write} onChange={(write) => setRule({ ...rule, write })}>
            書き込み可
          </Check>
          <Button
            onPress={() =>
              mutate(async (clientOpId) => {
                const req = {
                  appId,
                  clientOpId,
                  rule: { ...rule, id: ruleId, role: Number(rule.role) },
                };
                await createOrUpdate(
                  req,
                  (r) => api.createAccessRule(r),
                  (r) => api.updateAccessRule(r),
                );
              }).then((ok) => ok !== null && setRuleId(newId()))
            }
          >
            ルールを追加
          </Button>
        </div>
      </Section>
    </div>
  );
}
