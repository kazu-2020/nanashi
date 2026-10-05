import { Code, ConnectError } from "@connectrpc/connect";
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useRef } from "react";
import { errorText, newId } from "./api";
import type { ModelDef, Role } from "./gen/nanashi/v1/plan_pb";
import { METRIC, type Names } from "./logic";

export const Report = createContext<(e: unknown) => void>(() => {});

export type AppState = {
  appId: string;
  model: ModelDef;
  // The name of each list, member and Metric, by id.
  names: Names;
  can: (r: Role) => boolean;
};
export const AppCtx = createContext<AppState | null>(null);
export const useApp = () => useContext(AppCtx)!;

// modelNames gives the name of each list, member and Metric of the model, by id.
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
// definite error, and null after an ambiguous error (the api may have applied the write).
// It gives f one client_op_id for the user action. Send it with each write of the action.
// After an ambiguous error, it runs f again with the same client_op_id (up to 3 times). If the error stays
// ambiguous, the next user action of this component gets the same client_op_id again: the api then gives the
// stored result instead of a second write. A caller keeps its object id in the same way (a null result).
// After a success or a definite error, the next user action gets a new client_op_id.
export function useRun() {
  const report = useContext(Report);
  const held = useRef<string | null>(null);
  return useCallback(
    async (f: (clientOpId: string) => Promise<unknown>) => {
      const clientOpId = held.current ?? newId();
      held.current = null;
      for (let attempt = 1; ; attempt++) {
        try {
          await f(clientOpId);
          return true;
        } catch (e) {
          if (ambiguous(e) && attempt < 3) continue;
          report(errorText(e));
          if (!ambiguous(e)) return false;
          held.current = clientOpId;
          return null;
        }
      }
    },
    [report],
  );
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

// createOrUpdate runs create. If the id already exists (an earlier send of the same create committed), it runs update.
export async function createOrUpdate(
  create: () => Promise<unknown>,
  update: () => Promise<unknown>,
) {
  try {
    await create();
  } catch (e) {
    if (!(e instanceof ConnectError && e.code === Code.AlreadyExists)) throw e;
    await update();
  }
}

export const listOptions = (model: ModelDef) =>
  model.lists.map((l): [string, string] => [l.id, l.name]);

// memberOptions gives the members of the list, as [id, name].
export const memberOptions = (model: ModelDef, list: string) =>
  model.lists.find((l) => l.id === list)?.members.map((m): [string, string] => [m.id, m.name]) ??
  [];

export const cls = {
  table:
    "border-collapse text-sm [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:bg-gray-100 [&_th]:px-2 [&_th]:py-1",
};

export const time = (ms: bigint) => new Date(Number(ms)).toLocaleString("ja-JP");
