import type { ReactNode } from "react";
import { cls } from "./state";

export function Sel(props: {
  value: string;
  onChange: (v: string) => void;
  options: (string | [string, string])[];
  label?: string;
  empty?: string;
}) {
  return (
    <label className="inline-flex items-center gap-1 text-sm">
      {props.label}
      <select
        aria-label={props.label}
        className={cls.input}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
      >
        {props.empty !== undefined && <option value="">{props.empty}</option>}
        {props.options.map((o) => {
          const [v, l] = typeof o === "string" ? [o, o] : o;
          return (
            <option key={v} value={v}>
              {l}
            </option>
          );
        })}
      </select>
    </label>
  );
}

export function Checks(props: {
  options: string[];
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <span className="inline-flex flex-wrap gap-2 text-sm">
      {props.options.map((o) => (
        <label key={o} className="inline-flex items-center gap-1">
          <input
            type="checkbox"
            checked={props.value.includes(o)}
            onChange={(e) =>
              props.onChange(
                e.target.checked ? [...props.value, o] : props.value.filter((x) => x !== o),
              )
            }
          />
          {o}
        </label>
      ))}
    </span>
  );
}

export function Section(props: { title: string; children: ReactNode }) {
  return (
    <section className="mb-6 flex flex-col gap-2">
      <h2 className="text-lg font-bold">{props.title}</h2>
      {props.children}
    </section>
  );
}
