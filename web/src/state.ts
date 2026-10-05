import { Code, ConnectError } from "@connectrpc/connect";
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext } from "react";
import { errorText, newId } from "./api";
import type { ModelDef, Role } from "./gen/nanashi/v1/plan_pb";

export const Report = createContext<(e: unknown) => void>(() => {});

export type AppState = {
  appId: string;
  model: ModelDef;
  can: (r: Role) => boolean;
};
export const AppCtx = createContext<AppState | null>(null);
export const useApp = () => useContext(AppCtx)!;

// useRun gives a function that runs an action and shows its error. It returns true on success.
// It gives f one new client_op_id for the user action. Send it with each write of the action.
export function useRun() {
  const report = useContext(Report);
  return useCallback(
    async (f: (clientOpId: string) => Promise<unknown>) => {
      try {
        await f(newId());
        return true;
      } catch (e) {
        report(errorText(e));
        return false;
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

export const memberNames = (model: ModelDef, list: string) =>
  model.lists.find((l) => l.name === list)?.members.map((m) => m.name) ?? [];

export const cls = {
  table:
    "border-collapse text-sm [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:bg-gray-100 [&_th]:px-2 [&_th]:py-1",
};

export const time = (ms: bigint) => new Date(Number(ms)).toLocaleString("ja-JP");
