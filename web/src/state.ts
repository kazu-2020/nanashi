import { Code, ConnectError } from "@connectrpc/connect";
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useRef } from "react";
import { errorText, newId } from "./api";
import type { ModelDef, Role } from "./gen/nanashi/v1/plan_pb";
import { followOpId, METRIC, type Names } from "./logic";

export const Report = createContext<(e: unknown) => void>(() => {});

export type AppState = {
  appId: string;
  model: ModelDef;
  names: Names;
  can: (r: Role) => boolean;
};
export const AppCtx = createContext<AppState | null>(null);
export const useApp = () => useContext(AppCtx)!;

export function modelNames(model: ModelDef): Names {
  const out: Record<string, string> = { [METRIC]: "メトリック" };
  for (const l of model.lists) {
    out[l.id] = l.name;
    for (const m of l.members) out[m.id] = m.name;
  }
  for (const m of model.metrics) out[m.id] = m.name;
  return out;
}

// An error after which the api may have applied the write: send the same client_op_id again.
const ambiguous = (e: unknown) =>
  e instanceof ConnectError && (e.code === Code.Unavailable || e.code === Code.DeadlineExceeded);

// useRun gives a function that runs an action and shows its error. It returns true on success, false after a
// definite error or after it sent a held action instead, and null after an ambiguous error (the api may have
// applied the write).
// It gives f one client_op_id for the user action. Send it with each write of the action.
export function useRun() {
  const report = useContext(Report);
  const held = useRef<Held>(null);
  return useCallback((f: Action) => runner(report, held)(f), [report]);
}

type Action = (clientOpId: string) => Promise<unknown>;
type Held = { f: Action; clientOpId: string } | null;

// runner runs f with a new client_op_id. After an ambiguous error, it runs f again with the same client_op_id
// (up to 3 times). If the error stays ambiguous, it keeps f with its client_op_id in held. The next action then
// sends this original request again instead of its own f, so the api gives the stored result. A new f can
// make a different request (edited data, new object ids), and the api refuses it under the same client_op_id.
// After a success or a definite error, the next action runs its own f with a new client_op_id.
// If runner sends the held f, the next action does not run: runner tells the user and gives false.
// Thus put the success code of an action (for example, a new draft id) in f, not after runner.
export function runner(report: (e: unknown) => void, held: { current: Held }) {
  return async (next: Action) => {
    const resend = held.current !== null;
    const { f, clientOpId } = held.current ?? { f: next, clientOpId: newId() };
    held.current = null;
    for (let attempt = 1; ; attempt++) {
      try {
        await f(clientOpId);
        if (!resend) return true;
        report("前の操作を送り直した。今の操作はもう一度実行して");
        return false;
      } catch (e) {
        if (ambiguous(e) && attempt < 3) continue;
        report(errorText(e));
        if (!ambiguous(e)) return false;
        held.current = { f, clientOpId };
        return null;
      }
    }
  };
}

// useMutate runs a write, then reads again every query whose key starts with ["app", appId, part].
// Without part, it reads all the data of the application again. Give part only for a write that does not
// change the model, because the model is large. It also removes the queries that no screen shows.
// Otherwise a screen that opens later shows their old cells as editable while it reads them again.
export function useMutate(part?: "query" | "comments" | "snapshots") {
  const run = useRun();
  const { appId } = useApp();
  const queryClient = useQueryClient();
  const queryKey = part ? ["app", appId, part] : ["app", appId];
  return (f: (clientOpId: string) => Promise<unknown>) =>
    run(async (clientOpId) => {
      await f(clientOpId);
      await queryClient.invalidateQueries({ queryKey });
      queryClient.removeQueries({ queryKey, type: "inactive" });
    });
}

// createOrUpdate runs create. If create gives AlreadyExists, the id can exist (an earlier send of the same create
// committed) or the name can be taken. Then it runs update with its own client_op_id. If update gives NotFound, the
// id does not exist, so the create error goes to the user.
export async function createOrUpdate<R extends { clientOpId: string }>(
  req: R,
  create: (r: R) => Promise<unknown>,
  update: (r: R) => Promise<unknown>,
) {
  try {
    await create(req);
  } catch (e) {
    if (!(e instanceof ConnectError && e.code === Code.AlreadyExists)) throw e;
    try {
      await update({ ...req, clientOpId: followOpId(req.clientOpId) });
    } catch (u) {
      throw u instanceof ConnectError && u.code === Code.NotFound ? e : u;
    }
  }
}

export const listOptions = (model: ModelDef) =>
  model.lists.map((l): [string, string] => [l.id, l.name]);

export const metricOptions = (model: ModelDef) =>
  model.metrics.map((m): [string, string] => [m.id, m.name]);

export const memberOptions = (model: ModelDef, list: string) =>
  model.lists.find((l) => l.id === list)?.members.map((m): [string, string] => [m.id, m.name]) ??
  [];

export const cls = {
  table:
    "border-collapse text-sm [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:bg-gray-100 [&_th]:px-2 [&_th]:py-1",
};

export const time = (ms: bigint) => new Date(Number(ms)).toLocaleString("ja-JP");
