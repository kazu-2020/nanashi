import { create } from "@bufbuild/protobuf";
import { expect, test } from "vite-plus/test";
import { QueryResponseSchema } from "./gen/nanashi/v1/plan_pb";
import {
  buildGrid,
  formatValue,
  gridToCsv,
  mergeFilters,
  METRIC,
  parseCsv,
  parseValue,
  spreadable,
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
  expect(gridToCsv(g)).toBe("メトリック,B,A,(なし)\nSales,2,1,\nRate,,,9\n");
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
  const dims = ["Product", "Region"];
  expect(spreadable(dims, g, { Region: ["East"] }, "SUM")).toBe(true);
  expect(spreadable(dims, g, {}, "")).toBe(true);
  expect(spreadable(dims, g, { Region: ["East", "West"] }, "SUM")).toBe(false);
  for (const agg of ["AVG", "MIN", "MAX", "COUNT"])
    expect(spreadable(dims, g, {}, agg)).toBe(false);
});

test("a boolean cell takes TRUE, FALSE, 1 or 0 and refuses other text", () => {
  for (const [text, value] of [
    ["true", true],
    ["TRUE", true],
    ["1", true],
    [" False ", false],
    ["0", false],
  ] as const)
    expect(parseValue("boolean", text)).toEqual({ case: "boolean", value });
  expect(parseValue("boolean", " ")).toBeUndefined();
  for (const text of ["tru", "yes", "2"]) expect(() => parseValue("boolean", text)).toThrow(text);
});
