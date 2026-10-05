import { create } from "@bufbuild/protobuf";
import { expect, test } from "vite-plus/test";
import { Aggregation, QueryResponseSchema, ValueKind } from "./gen/nanashi/v1/plan_pb";
import {
  buildGrid,
  dropsInputCells,
  editable,
  formatValue,
  gridToCsv,
  mergeFilters,
  METRIC,
  parseCsv,
  parseValue,
  uuidv7,
  writeCoords,
} from "./logic";

const num = (n: number) => ({ value: { case: "number" as const, value: n } });

test("grid follows member order and puts Metrics on an axis", () => {
  const resp = create(QueryResponseSchema, {
    dimensions: ["Product"],
    cells: [
      { metric: "Sales", coords: ["B"], value: num(2) },
      { metric: "Sales", coords: ["A"], value: num(1) },
      { metric: "Rate", coords: [""], value: num(9) },
    ],
  });
  const g = buildGrid(resp, ["Sales", "Rate"], [METRIC], ["Product"], { Product: ["B", "A"] }, {});
  expect(g.rowKeys).toEqual([["Sales"], ["Rate"]]);
  expect(g.colKeys).toEqual([["B"], ["A"], [""]]);
  expect(formatValue(g.value(["Sales"], ["A"]))).toBe("1");
  expect(formatValue(g.value(["Rate"], [""]))).toBe("9");
  const names = { [METRIC]: "メトリック", Sales: "売上", Rate: "率", A: "a", B: "b" };
  expect(gridToCsv(g, names)).toBe("メトリック,b,a,(なし)\n売上,2,1,\n率,,,9\n");
});

test("edit coords leave unshown dimensions open for a spread", () => {
  const resp = create(QueryResponseSchema, { dimensions: ["Product"], cells: [] });
  const g = buildGrid(resp, ["Sales"], ["Product"], [], { Product: ["A"] }, {});
  expect(writeCoords(["Product", "Month", "Region"], g, ["A"], [], { Region: ["East"] })).toEqual({
    Product: "A",
    Region: "East",
  });
});

test("csv parse handles quotes and page selectors override view filters", () => {
  expect(parseCsv('a,"b,""c"""\r\n1,2')).toEqual([
    ["a", 'b,"c"'],
    ["1", "2"],
  ]);
  expect(mergeFilters({ A: ["x", "y"], B: ["z"] }, { A: "y", C: "" })).toEqual({
    A: ["y"],
    B: ["z"],
  });
});

test("an edit is refused if its spread goes outside the filter or the aggregation is not SUM", () => {
  const resp = create(QueryResponseSchema, { dimensions: ["Product"], cells: [] });
  const g = buildGrid(resp, ["Sales"], ["Product"], [], {}, {});
  const def = { dimensions: ["Product", "Region"], formula: "", overridable: false };
  expect(editable(def, g, { Region: ["East"] }, Aggregation.SUM)).toBe(true);
  expect(editable(def, g, {}, Aggregation.UNSPECIFIED)).toBe(true);
  expect(editable(def, g, { Region: ["East", "West"] }, Aggregation.SUM)).toBe(false);
  for (const agg of [Aggregation.AVG, Aggregation.MIN, Aggregation.MAX, Aggregation.COUNT])
    expect(editable(def, g, {}, agg)).toBe(false);
});

test("an edit is refused for an unknown Metric and for a formula Metric that is not overridable", () => {
  const resp = create(QueryResponseSchema, { dimensions: ["Product"], cells: [] });
  const g = buildGrid(resp, ["Sales"], ["Product"], [], {}, {});
  const def = { dimensions: ["Product"], formula: "Price * Units", overridable: false };
  expect(editable(undefined, g, {}, Aggregation.SUM)).toBe(false);
  expect(editable(def, g, {}, Aggregation.SUM)).toBe(false);
  expect(editable({ ...def, overridable: true }, g, {}, Aggregation.SUM)).toBe(true);
});

test("a boolean cell takes TRUE, FALSE, 1 or 0 and refuses other text", () => {
  for (const [text, value] of [
    ["true", true],
    ["TRUE", true],
    ["1", true],
    [" False ", false],
    ["0", false],
  ] as const)
    expect(parseValue(ValueKind.BOOLEAN, text)).toEqual({ case: "boolean", value });
  expect(parseValue(ValueKind.BOOLEAN, " ")).toBeUndefined();
  for (const text of ["tru", "yes", "2"])
    expect(() => parseValue(ValueKind.BOOLEAN, text)).toThrow(text);
});

test("a cell text gives the value of the Metric kind", () => {
  expect(parseValue(ValueKind.MEMBER, " 12 ")).toEqual({ case: "member", value: "12" });
  expect(parseValue(ValueKind.NUMBER, "12")).toEqual({ case: "number", value: 12 });
  expect(() => parseValue(ValueKind.NUMBER, "x")).toThrow("x");
});

test("uuidv7 has the format, the version, the variant and the time order", () => {
  const ones = new Uint8Array(16).fill(0xff);
  const id = uuidv7(0x0192_f3a4_5b6c, ones);
  expect(id).toBe("0192f3a4-5b6c-7fff-bfff-ffffffffffff");
  const zeros = uuidv7(1, new Uint8Array(16));
  expect(zeros).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(zeros).toBe("00000000-0001-7000-8000-000000000000");
  // A later millisecond sorts after an earlier one, whatever the random bytes are.
  const ms = Date.UTC(2026, 9, 5);
  const ids = [0, 1, 255, 256, 65_536].map((d) =>
    uuidv7(ms + d, d % 2 ? ones : new Uint8Array(16)),
  );
  expect([...ids].sort()).toEqual(ids);
  expect(uuidv7(ms + 1, new Uint8Array(16)) > uuidv7(ms, ones)).toBe(true);
});

test("only a change that makes an input Metric again deletes its cells", () => {
  const input = { formula: "", kind: ValueKind.NUMBER, memberList: "", dimensions: ["A", "B"] };
  expect(dropsInputCells(input, input)).toBe(false);
  expect(dropsInputCells(input, { ...input, dimensions: ["B", "A"] })).toBe(true);
  expect(dropsInputCells(input, { ...input, formula: "1" })).toBe(true);
  expect(dropsInputCells(input, { ...input, kind: ValueKind.BOOLEAN })).toBe(true);
  expect(dropsInputCells({ ...input, formula: "1" }, { ...input, formula: "2" })).toBe(false);
});
