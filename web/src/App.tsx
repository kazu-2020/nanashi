import { Alert, Button, CloseButton, Input } from "@heroui/react";
import { QueryCache, QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import {
  Component,
  lazy,
  Suspense,
  useCallback,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import { api, errorText, getUser, setUser } from "./api";
import { Role } from "./gen/nanashi/v1/plan_pb";
import { roleName } from "./logic";
import { AppCtx, Report, useRun } from "./state";
import { Sel } from "./ui";

const ListsPage = lazy(() => import("./pages/model").then((m) => ({ default: m.ListsPage })));
const CalendarPage = lazy(() => import("./pages/model").then((m) => ({ default: m.CalendarPage })));
const ScenariosPage = lazy(() =>
  import("./pages/model").then((m) => ({ default: m.ScenariosPage })),
);
const ImportPage = lazy(() => import("./pages/model").then((m) => ({ default: m.ImportPage })));
const MetricsPage = lazy(() => import("./pages/metrics").then((m) => ({ default: m.MetricsPage })));
const TablesPage = lazy(() => import("./pages/metrics").then((m) => ({ default: m.TablesPage })));
const ViewsPage = lazy(() => import("./pages/metrics").then((m) => ({ default: m.ViewsPage })));
const BoardsPage = lazy(() => import("./pages/boards").then((m) => ({ default: m.BoardsPage })));
const CommentsPage = lazy(() => import("./pages/admin").then((m) => ({ default: m.CommentsPage })));
const AuditPage = lazy(() => import("./pages/admin").then((m) => ({ default: m.AuditPage })));
const SnapshotsPage = lazy(() =>
  import("./pages/admin").then((m) => ({ default: m.SnapshotsPage })),
);
const AccessPage = lazy(() => import("./pages/admin").then((m) => ({ default: m.AccessPage })));

// PAGES gives the navigation. A page with a role shows only to users with that role or higher.
const PAGES: { id: string; label: string; page: ComponentType; role?: Role }[] = [
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
  const [user, setCurrentUser] = useState(getUser());
  const [app, setApp] = useState<{ id: string; name: string }>();
  const report = useCallback((e: unknown) => setError(errorText(e)), []);
  // A failed read shows in the error banner. A retry only delays the message.
  const [queries] = useState(
    () =>
      new QueryClient({
        queryCache: new QueryCache({ onError: report }),
        defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
      }),
  );
  return (
    <QueryClientProvider client={queries}>
      <Report.Provider value={report}>
        <main className="mx-auto flex max-w-7xl flex-col gap-4 p-4">
          <header className="flex items-center gap-4">
            <h1 className="text-xl font-bold">nanashi</h1>
            {app && (
              <Button size="sm" variant="ghost" onPress={() => setApp(undefined)}>
                アプリケーション一覧
              </Button>
            )}
            {app && <span className="font-bold">{app.name}</span>}
            {user && (
              <span className="ml-auto text-sm">
                {user}{" "}
                <Button
                  size="sm"
                  variant="ghost"
                  onPress={() => {
                    setUser("");
                    setCurrentUser("");
                    setApp(undefined);
                    // The next user must not see the data of this user.
                    queries.clear();
                  }}
                >
                  ログアウト
                </Button>
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
                setCurrentUser(u);
              }}
            />
          ) : !app ? (
            <Apps onOpen={setApp} />
          ) : (
            <Shell key={app.id} appId={app.id} />
          )}
        </main>
      </Report.Provider>
    </QueryClientProvider>
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
  const { data: apps } = useQuery({
    queryKey: ["apps"],
    queryFn: () => api.listApplications({}),
  });
  const [name, setName] = useState("");
  const [from, setFrom] = useState("");
  const [snapshot, setSnapshot] = useState("");
  const { data: snaps } = useQuery({
    queryKey: ["app", from, "snapshots"],
    queryFn: () => api.listSnapshots({ appId: from }),
    enabled: !!from,
  });
  const create = (snapshotId: string) =>
    run(async () => props.onOpen(await api.createApplication({ name, snapshotId })));
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-lg font-bold">アプリケーション</h2>
      <ul className="flex flex-col gap-1">
        {apps?.applications.map((a) => (
          <li key={a.id}>
            <Button size="sm" variant="ghost" onPress={() => props.onOpen(a)}>
              {a.name}
            </Button>{" "}
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
          onChange={(v) => {
            setFrom(v);
            setSnapshot("");
          }}
          empty=""
          options={apps?.applications.map((a): [string, string] => [a.id, a.name]) ?? []}
        />
        <Sel
          label="スナップショット"
          value={snapshot}
          onChange={setSnapshot}
          empty=""
          options={snaps?.snapshots.map((s): [string, string] => [s.id, s.name]) ?? []}
        />
        <Button isDisabled={!snapshot} onPress={() => create(snapshot)}>
          新しいアプリケーションに復元
        </Button>
      </div>
    </div>
  );
}

function Shell(props: { appId: string }) {
  const { data: model } = useQuery({
    queryKey: ["app", props.appId, "model"],
    queryFn: () => api.getModel({ appId: props.appId }),
  });
  const [page, setPage] = useState("lists");
  if (!model) return <p>読み込み中…</p>;
  const can = (r: Role) => model.role >= r;
  const pages = PAGES.filter((p) => !p.role || can(p.role));
  const Page = (pages.find((p) => p.id === page) ?? pages[0]).page;
  return (
    <AppCtx.Provider value={{ appId: props.appId, model, can }}>
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
          <PageBoundary key={page}>
            <Suspense fallback={<p>読み込み中…</p>}>
              <Page />
            </Suspense>
          </PageBoundary>
        </div>
      </div>
    </AppCtx.Provider>
  );
}

// PageBoundary catches an error of a screen, for example a chunk that did not download after a deploy.
// The key of the screen clears the message when the user opens another screen. React.lazy keeps a failed
// import, so the same screen fails again until the user reloads the page.
class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="flex flex-col items-start gap-2">
        <p>画面を表示できませんでした。</p>
        <Button size="sm" onPress={() => location.reload()}>
          再読み込み
        </Button>
      </div>
    );
  }
}
