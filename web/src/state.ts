// Shared React context and hooks.
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { errorText } from "./api";
import type { ModelDef, Role } from "./gen/nanashi/v1/plan_pb";

export const Report = createContext<(e: unknown) => void>(() => {});

export type AppState = {
  appId: string;
  model: ModelDef;
  reload: () => Promise<void>;
  can: (r: Role) => boolean;
};
export const AppCtx = createContext<AppState | null>(null);
export const useApp = () => useContext(AppCtx)!;

// useRun gives a function that runs an action and shows its error. It returns true on success.
export function useRun() {
  const report = useContext(Report);
  return useCallback(
    async (f: () => Promise<unknown>) => {
      try {
        await f();
        return true;
      } catch (e) {
        report(errorText(e));
        return false;
      }
    },
    [report],
  );
}

// useLoad reads data when deps change. The second value reads again.
export function useLoad<T>(f: () => Promise<T>, deps: unknown[]): [T | undefined, () => void] {
  const report = useContext(Report);
  const [data, setData] = useState<T>();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    f().then(
      (d) => live && setData(d),
      (e) => live && report(errorText(e)),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return [data, () => setTick((t) => t + 1)];
}

export const cls = {
  input: "rounded border border-gray-300 px-2 py-1 text-sm",
  table:
    "border-collapse text-sm [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:bg-gray-100 [&_th]:px-2 [&_th]:py-1",
};

export const time = (ms: bigint) => new Date(Number(ms)).toLocaleString("ja-JP");
