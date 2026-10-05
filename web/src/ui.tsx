import { Checkbox, CheckboxGroup, Label, ListBox, Select } from "@heroui/react";
import type { ComponentProps, ReactNode } from "react";

// React Aria does not accept "" as an item key, so the empty option uses this key.
const EMPTY = "\u0000";

export function Sel(props: {
  value: string;
  onChange: (v: string) => void;
  options: (string | [string, string])[];
  label?: string;
  // ariaLabel names a Sel that shows no label.
  ariaLabel?: string;
  empty?: string;
}) {
  const items = props.options.map((o) => (typeof o === "string" ? [o, o] : o));
  if (props.empty !== undefined) items.unshift([EMPTY, props.empty]);
  return (
    <Select
      aria-label={props.label ? undefined : props.ariaLabel}
      className="w-auto min-w-36"
      placeholder=""
      value={props.value === "" ? EMPTY : props.value}
      onChange={(k) => props.onChange(k === EMPTY ? "" : String(k ?? ""))}
    >
      {props.label && <Label>{props.label}</Label>}
      <Select.Trigger>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      <Select.Popover>
        <ListBox>
          {items.map(([v, l]) => (
            <ListBox.Item key={v} id={v} textValue={l || " "}>
              {l}
              <ListBox.ItemIndicator />
            </ListBox.Item>
          ))}
        </ListBox>
      </Select.Popover>
    </Select>
  );
}

export function Check({
  children,
  ...rest
}: Omit<ComponentProps<typeof Checkbox>, "children"> & { children: ReactNode }) {
  return (
    <Checkbox {...rest}>
      <Checkbox.Content>
        <Checkbox.Control>
          <Checkbox.Indicator />
        </Checkbox.Control>
        {children}
      </Checkbox.Content>
    </Checkbox>
  );
}

export function Checks(props: {
  // [value, label] pairs.
  options: [string, string][];
  value: string[];
  onChange: (v: string[]) => void;
  label: string;
}) {
  return (
    <CheckboxGroup
      aria-label={props.label}
      className="flex flex-row flex-wrap gap-3 text-sm"
      value={props.value}
      onChange={props.onChange}
    >
      {props.options.map(([v, l]) => (
        <Check key={v} value={v}>
          {l}
        </Check>
      ))}
    </CheckboxGroup>
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
