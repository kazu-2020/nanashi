package api

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestImportTransactionList(t *testing.T) {
	em := model(t)
	meta := appMeta{Props: []propRow{
		{List: "Sales", Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER},
		{List: "Sales", Name: "Note", Type: nanashiv1.PropertyType_PROPERTY_TYPE_TEXT, Text: map[string]string{"1": "old"}},
	}}
	csv := "product,amount,note\nA,\"1,200\",first\nB,5,\n"
	edits, rows, err := importListEdits(csv, &nanashiv1.ListImport{List: "Sales",
		PropertyColumns: map[string]string{"Product": "product", "Amount": "amount", "Note": "note"}}, em, nanashiv1.ListKind_LIST_KIND_TRANSACTION)
	if err != nil || rows != 2 {
		t.Fatal(rows, err)
	}
	ops, stmts, err := editOps("app", "Sales", em, meta, edits)
	if err != nil {
		t.Fatal(err)
	}
	// New rows continue after the largest number (9). One operation and one statement set the values of all rows.
	want := `[{"op":"add_member","args":["Sales","10"]},{"op":"set_cell","args":["Sales.Amount",1200],"kwargs":{"Sales":"10"}},` +
		`{"op":"add_member","args":["Sales","11"]},{"op":"set_cell","args":["Sales.Amount",5],"kwargs":{"Sales":"11"}},` +
		`{"op":"set_property_values","args":["Sales","Product",{"10":"A","11":"B"}]}]`
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s", got)
	}
	// Row 10 sets its Note, and the blank Note of row 11 removes the value. The other values stay in the table.
	if len(stmts) != 1 || fmt.Sprint(stmts[0].args) != `[app Sales Note [11] {"10":"first"}]` {
		t.Errorf("text statements: %v", stmts)
	}
}

func TestImportMetric(t *testing.T) {
	em := model(t)
	imp := &nanashiv1.MetricImport{Metric: "Budget", DimensionColumns: map[string]string{"Product": "p", "Region": "r"}, ValueColumn: "v"}
	csv := "p,r,v\nA,East,3\nC,East,4\n"
	if _, _, err := importMetricOps(csv, imp, em, limits{}); err == nil || !strings.Contains(err.Error(), "3 行目") {
		t.Errorf("unknown member: got %v", err)
	}
	imp.AddMembers = true
	ops, rows, err := importMetricOps(csv, imp, em, limits{})
	if err != nil || rows != 2 {
		t.Fatal(rows, err)
	}
	want := `[{"op":"add_member","args":["Product","C"]},{"op":"set_cell","args":["Budget",3],"kwargs":{"Product":"A","Region":"East"}},` +
		`{"op":"set_cell","args":["Budget",4],"kwargs":{"Product":"C","Region":"East"}}]`
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s", got)
	}
	if _, _, err := importMetricOps("p,r,v\nA,West,1\n", imp, em, eastOnly()); !errors.Is(err, errDenied) {
		t.Errorf("a row outside the access rule: got %v, want a permission error", err)
	}
}
