import { Alert, Button, CloseButton, Input } from "@heroui/react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, errorText, getUser, setUser } from "./api";
import { type ModelDef, Role } from "./gen/nanashi/v1/plan_pb";
import {
  AccessPage,
  AuditPage,
  BoardsPage,
  CalendarPage,
  CommentsPage,
  ImportPage,
  ListsPage,
  MetricsPage,
  ScenariosPage,
  SnapshotsPage,
  TablesPage,
  ViewsPage,
} from "./pages";
import { ROLES } from "./logic";
import { AppCtx, Report, useLoad, useRun } from "./state";
import { Sel } from "./ui";

// PAGES gives the navigation. A page with a role shows only to users with that role or higher.
const PAGES: { id: string; label: string; page: () => ReactNode; role?: Role }[] = [
  { id: "lists", label: "リスト", page: ListsPage },
  { id: "metrics", label: "メトリック", page: MetricsPage },
  { id: "tables", label: "テーブル", page: TablesPage },
  { id: "views", label: "ビュー", page: ViewsPage },
  { id: "boards", label: "ボード", page: BoardsPage },
  { id: "import", label: "インポート", page: ImportPage, role: Role.CONTRIBUTOR },
  { id: "scenarios", label: "シナリオ", page: ScenariosPage },
  { id: "calendar", label: "カレンダー", page: CalendarPage },
  { id: "comments", label: "コメント", page: CommentsPage },
  { id: "audit", label: "監査ログ", page: AuditPage, role: Role.MODELER },
  { id: "snapshots", label: "スナップショット", page: SnapshotsPage },
  { id: "access", label: "アクセス権", page: AccessPage, role: Role.ADMIN },
];

export default function App() {
  const [error, setError] = useState("");
  const [user, setU] = useState(getUser());
  const [app, setApp] = useState<{ id: string; name: string }>();
  const report = useCallback((e: unknown) => setError(errorText(e)), []);
  return (
    <Report.Provider value={report}>
      <main className="mx-auto flex max-w-7xl flex-col gap-4 p-4">
        <header className="flex items-center gap-4">
          <h1 className="text-xl font-bold">nanashi</h1>
          {app && (
            <button className="text-sm underline" onClick={() => setApp(undefined)}>
              アプリケーション一覧
            </button>
          )}
          {app && <span className="font-bold">{app.name}</span>}
          {user && (
            <span className="ml-auto text-sm">
              {user}{" "}
              <button
                className="underline"
                onClick={() => {
                  setUser("");
                  setU("");
                  setApp(undefined);
                }}
              >
                ログアウト
              </button>
            </span>
          )}
        </header>
        {error && (
          <Alert status="danger">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Title>{error}</Alert.Title>
            </Alert.Content>
            <CloseButton aria-label="閉じる" onPress={() => setError("")} />
          </Alert>
        )}
        {!user ? (
          <Login
            onLogin={(u) => {
              setUser(u);
              setU(u);
            }}
          />
        ) : !app ? (
          <Apps onOpen={setApp} />
        ) : (
          <Shell key={app.id} appId={app.id} />
        )}
      </main>
    </Report.Provider>
  );
}

function Login(props: { onLogin: (u: string) => void }) {
  const [name, setName] = useState("");
  const ok = /^[\x21-\x7e]+$/.test(name);
  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (ok) props.onLogin(name);
      }}
    >
      <p>ユーザー名を入力してください（半角英数字と記号）。</p>
      <div className="flex gap-2">
        <Input aria-label="ユーザー名" value={name} onChange={(e) => setName(e.target.value)} />
        <Button type="submit" isDisabled={!ok}>
          ログイン
        </Button>
      </div>
    </form>
  );
}

function Apps(props: { onOpen: (a: { id: string; name: string }) => void }) {
  const run = useRun();
  const [apps] = useLoad(() => api.listApplications({}), []);
  const [name, setName] = useState("");
  const [from, setFrom] = useState("");
  const [snapshot, setSnapshot] = useState("");
  const [snaps] = useLoad(
    async () => (from ? (await api.listSnapshots({ appId: from })).snapshots : []),
    [from],
  );
  const create = (snapshotId: string) =>
    run(async () => props.onOpen(await api.createApplication({ name, snapshotId })));
  const roleName = (r: Role) => ROLES.find(([v]) => v === String(r))?.[1];
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-lg font-bold">アプリケーション</h2>
      <ul className="flex flex-col gap-1">
        {apps?.applications.map((a) => (
          <li key={a.id}>
            <button className="underline" onClick={() => props.onOpen(a)}>
              {a.name}
            </button>{" "}
            <span className="text-sm">（{roleName(a.role)}）</span>
          </li>
        ))}
        {apps?.applications.length === 0 && <li>アプリケーションがありません。</li>}
      </ul>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          aria-label="アプリケーション名"
          placeholder="アプリケーション名"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Button onPress={() => create("")}>作成</Button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-bold">スナップショットから復元:</span>
        <Sel
          label="アプリケーション"
          value={from}
          onChange={(v) => (setFrom(v), setSnapshot(""))}
          empty=""
          options={apps?.applications.map((a): [string, string] => [a.id, a.name]) ?? []}
        />
        <Sel
          label="スナップショット"
          value={snapshot}
          onChange={setSnapshot}
          empty=""
          options={snaps?.map((s): [string, string] => [s.id, s.name]) ?? []}
        />
        <Button isDisabled={!snapshot} onPress={() => create(snapshot)}>
          新しいアプリケーションに復元
        </Button>
      </div>
    </div>
  );
}

function Shell(props: { appId: string }) {
  const run = useRun();
  const [model, setModel] = useState<ModelDef>();
  const [page, setPage] = useState("lists");
  const reload = useCallback(async () => {
    await run(async () => setModel(await api.getModel({ appId: props.appId })));
  }, [run, props.appId]);
  useEffect(() => {
    void reload();
  }, [reload]);
  if (!model) return <p>読み込み中…</p>;
  const can = (r: Role) => model.role >= r;
  const pages = PAGES.filter((p) => !p.role || can(p.role));
  const Page = (pages.find((p) => p.id === page) ?? pages[0]).page;
  return (
    <AppCtx.Provider value={{ appId: props.appId, model, reload, can }}>
      <div className="flex gap-4">
        <nav className="flex w-40 shrink-0 flex-col gap-1">
          {pages.map((p) => (
            <Button
              key={p.id}
              size="sm"
              variant={p.id === page ? "primary" : "ghost"}
              onPress={() => setPage(p.id)}
            >
              {p.label}
            </Button>
          ))}
        </nav>
        <div className="min-w-0 flex-1">
          <Page />
        </div>
      </div>
    </AppCtx.Provider>
  );
}
