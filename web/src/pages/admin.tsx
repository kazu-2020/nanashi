import { Button, Input } from "@heroui/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, newId } from "../api";
import { label } from "../logic";
import { cls, time, useApp, useMutate } from "../state";
import { Section } from "../ui";

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
  const { appId } = useApp();
  const mutate = useMutate("snapshots");
  const [name, setName] = useState("");
  const [id, setId] = useState(newId);
  const { data: r } = useQuery({
    queryKey: ["app", appId, "snapshots"],
    queryFn: () => api.listSnapshots({ appId }),
  });
  return (
    <Section title="スナップショット">
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
              setId(newId());
            })
          }
        >
          作成
        </Button>
      </div>
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
