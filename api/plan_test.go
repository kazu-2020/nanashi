package api

import (
	"encoding/json"
	"strings"
	"testing"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// sample is an engine model (GET /) with a transaction list Sales that maps to Product.
const sample = `{"seq": 7,
 "dimensions": {
  "Product": {"members": ["B", "A"], "ordered": false, "properties": {}, "property_values": {}},
  "Sales": {"members": ["1", "2", "9"], "ordered": false, "properties": {"Product": "Product"},
            "property_values": {"Product": {"1": "A", "2": "B"}}},
  "Region": {"members": ["East", "West"], "ordered": false, "properties": {}, "property_values": {}}},
 "metrics": {
  "Sales.Amount": {"dims": ["Sales"], "kind": "number", "overridable": false, "formula": null},
  "Budget": {"dims": ["Product", "Region"], "kind": "number", "overridable": false, "formula": null},
  "Revenue": {"dims": ["Product"], "kind": "number", "overridable": false, "formula": "'Sales.Amount'[BY SUM: Sales.Product]"},
  "Owner": {"dims": ["Product"], "kind": "member:Region", "overridable": false, "formula": null}}}`

func model(t *testing.T) engineModel {
	t.Helper()
	em, err := parseEngineModel([]byte(sample))
	if err != nil {
		t.Fatal(err)
	}
	return em
}

func opsJSON(t *testing.T, ops []op) string {
	t.Helper()
	b, err := json.Marshal(ops)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// eastOnly is a CONTRIBUTOR limited to East (read and write).
func eastOnly() limits {
	return accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: "Region", Members: []string{"East"}, Write: true}})
}

func TestParseEngineModelKeepsOrder(t *testing.T) {
	em := model(t)
	var dims, metrics []string
	for _, d := range em.Dims {
		dims = append(dims, d.Name)
	}
	for _, m := range em.Metrics {
		metrics = append(metrics, m.Name)
	}
	if got := strings.Join(dims, ",") + "|" + strings.Join(metrics, ","); got != "Product,Sales,Region|Sales.Amount,Budget,Revenue,Owner" {
		t.Errorf("order: %s", got)
	}
	sales, _ := em.dim("Sales")
	if p := sales.Props[0]; p.Target != "Product" || p.Values["2"] != "B" {
		t.Errorf("property: %+v", p)
	}
}

func TestAccessLimits(t *testing.T) {
	rules := []*nanashiv1.AccessRule{
		{Role: viewer, List: "Region", Members: []string{"East", "West"}, Write: true},
		{Role: viewer, List: "Region", Members: []string{"East"}},
		{Role: contributor, List: "Product", Members: []string{"A"}},
	}
	l := accessLimits(viewer, rules)
	if !l.visible("Region", "East") || l.visible("Region", "West") || !l.visible("Product", "B") {
		t.Errorf("read limits: %+v", l)
	}
	if l["Region"].write["East"] {
		t.Error("a read-only rule must remove the write right")
	}
	if len(accessLimits(modeler, rules)) != 0 {
		t.Error("a MODELER must ignore the rules")
	}
}

func TestLimitsThroughProperties(t *testing.T) {
	em := engineModel{Dims: []engineDim{
		{Name: "Product", Members: []string{"A", "B"}},
		{Name: "Sales", Members: []string{"1", "2", "3"},
			Props: []engineProp{{Name: "Product", Target: "Product", Values: map[string]string{"1": "A", "2": "B"}}}},
		{Name: "Line", Members: []string{"x", "y"},
			Props: []engineProp{{Name: "Sale", Target: "Sales", Values: map[string]string{"x": "1", "y": "2"}}}},
	}}
	l := accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: "Product", Members: []string{"A"}, Write: true}}).through(em)
	if !l.visible("Sales", "1") || l.visible("Sales", "2") || l.visible("Sales", "3") {
		t.Errorf("rows of other or blank products must be hidden: %+v", l["Sales"])
	}
	if !l.visible("Line", "x") || l.visible("Line", "y") || !l["Line"].write["x"] {
		t.Errorf("the limit must follow a chain of properties: %+v", l["Line"])
	}
}

func TestWriteOps(t *testing.T) {
	em := model(t)
	num := func(v float64) *nanashiv1.Value { return &nanashiv1.Value{Value: &nanashiv1.Value_Number{Number: v}} }
	ops, err := writeOps([]*nanashiv1.CellWrite{
		{Metric: "Budget", Coords: map[string]string{"Product": "A", "Region": "East"}, Value: num(5)},
		{Metric: "Budget", Coords: map[string]string{"Region": "East"}, Value: num(100)},
		{Metric: "Budget", Coords: map[string]string{"Product": "B", "Region": "East"}},
	}, em, eastOnly())
	if err != nil {
		t.Fatal(err)
	}
	want := `[{"op":"set_cell","args":["Budget",5],"kwargs":{"Product":"A","Region":"East"}},` +
		`{"op":"spread","args":["Budget",100],"kwargs":{"Region":"East"}},` +
		`{"op":"set_cell","args":["Budget",null],"kwargs":{"Product":"B","Region":"East"}}]`
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s", got)
	}
	for name, coords := range map[string]map[string]string{
		"outside the rule":  {"Product": "A", "Region": "West"},
		"spread over rules": {"Product": "A"},
	} {
		if _, err := writeOps([]*nanashiv1.CellWrite{{Metric: "Budget", Coords: coords, Value: num(1)}}, em, eastOnly()); err == nil {
			t.Errorf("%s: want an error", name)
		}
	}
}

func TestQueryReads(t *testing.T) {
	em := model(t)
	req := &nanashiv1.QueryRequest{Metrics: []string{"Budget", "Revenue", "Owner"}, Rows: []string{"Product"}, Columns: []string{"Region"},
		Filters: map[string]*nanashiv1.Members{"Product": {Names: []string{"A"}}}}
	reads, err := queryReads(req, em, eastOnly())
	if err != nil {
		t.Fatal(err)
	}
	got, _ := json.Marshal(reads)
	want := `[{"Metric":"Budget","Path":"summary","Query":{"Product":["A"],"Region":["East"],"agg":["sum"],"keep":["Product,Region"]}},` +
		`{"Metric":"Revenue","Path":"summary","Query":{"Product":["A"],"agg":["sum"],"keep":["Product"]}},` +
		`{"Metric":"Owner","Path":"slice","Query":{"Product":["A"]}}]`
	if string(got) != want {
		t.Errorf("got %s", got)
	}
	// A filter outside the rule leaves no member, so the Metric is left out.
	req.Filters["Region"] = &nanashiv1.Members{Names: []string{"West"}}
	if reads, _ := queryReads(req, em, eastOnly()); len(reads) != 2 || reads[0].Metric != "Revenue" {
		t.Errorf("got %+v", reads)
	}
	// A removed member in a filter is dropped, not sent to the engine.
	req.Filters = map[string]*nanashiv1.Members{"Product": {Names: []string{"A", "Gone"}}}
	if reads, _ := queryReads(req, em, nil); reads[0].Query["Product"][0] != "A" {
		t.Errorf("got %+v", reads)
	}
}

func TestQueryCells(t *testing.T) {
	cube := engineCube{Dims: []string{"Product"}, Cells: [][]any{{"A", 11.0}, {"B", nil}}}
	cells := queryCells("Revenue", []string{"Region", "Product"}, cube)
	if len(cells) != 1 || strings.Join(cells[0].Coords, ",") != ",A" || cells[0].Value.GetNumber() != 11 {
		t.Errorf("got %v", cells)
	}
}

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
	ops, text, err := editOps("Sales", em, meta, edits)
	if err != nil {
		t.Fatal(err)
	}
	// New rows continue after the largest number (9). add_property sends the whole mapping.
	want := `[{"op":"add_member","args":["Sales","10"]},{"op":"set_cell","args":["Sales.Amount",1200],"kwargs":{"Sales":"10"}},` +
		`{"op":"add_member","args":["Sales","11"]},{"op":"set_cell","args":["Sales.Amount",5],"kwargs":{"Sales":"11"}},` +
		`{"op":"add_property","args":["Sales","Product","Product",{"1":"A","10":"A","11":"B","2":"B"}]}]`
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s", got)
	}
	if n := text["Note"]; n["1"] != "old" || n["10"] != "first" || len(n) != 2 {
		t.Errorf("text: %v", n)
	}
}

func TestEditMembersRenameRemove(t *testing.T) {
	em := model(t)
	ops, _, err := editOps("Sales", em, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Rename{Rename: &nanashiv1.RenameMember{Name: "1", NewName: "one"}}},
		{Edit: &nanashiv1.MemberEdit_Remove{Remove: &nanashiv1.RemoveMember{Name: "2"}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Name: "9", Properties: map[string]string{"Product": "B"}}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := `[{"op":"rename_member","args":["Sales","1","one"]},{"op":"remove_member","args":["Sales","2"]},` +
		`{"op":"add_property","args":["Sales","Product","Product",{"9":"B","one":"A"}]}]`
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s", got)
	}
	if _, _, err := editOps("Sales", em, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Name: "1", Properties: map[string]string{"Color": "x"}}}},
	}); err == nil {
		t.Error("an unknown property must be an error")
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
	if _, _, err := importMetricOps("p,r,v\nA,West,1\n", imp, em, eastOnly()); err == nil {
		t.Error("a row outside the access rule must be an error")
	}
}

func TestCalendarOps(t *testing.T) {
	ops, err := calendarOps(2026, 2)
	if err != nil {
		t.Fatal(err)
	}
	months := ops[2].Args[1].([]string)
	quarterOf := ops[3].Args[3].(map[string]string)
	yearOfQuarter := ops[5].Args[3].(map[string]string)
	if len(months) != 24 || months[0] != "2026-01" || months[23] != "2027-12" ||
		quarterOf["2026-05"] != "2026-Q2" || yearOfQuarter["2027-Q4"] != "2027" || ops[0].Kwargs["ordered"] != true {
		t.Errorf("got %s", opsJSON(t, ops))
	}
	if _, err := calendarOps(2026, 0); err == nil {
		t.Error("0 years must be an error")
	}
}

func TestReplayOps(t *testing.T) {
	em := model(t)
	inputs := map[string]engineCube{
		"Budget":       {Dims: []string{"Region", "Product"}, Cells: [][]any{{"East", "A", 5.0}}},
		"Sales.Amount": {Dims: []string{"Sales"}, Cells: [][]any{{"1", 10.0}}},
	}
	got := opsJSON(t, replayOps(em, inputs))
	for _, want := range []string{
		`{"op":"add_dimension","args":["Sales",["1","2","9"]],"kwargs":{"ordered":false}}`,
		`{"op":"add_property","args":["Sales","Product","Product",{"1":"A","2":"B"}]}`,
		`{"op":"add_input","args":["Budget",["Product","Region"],[[["A","East"],5]]],"kwargs":{"kind":"number"}}`,
		`{"op":"add_formula","args":["Revenue",["Product"],"'Sales.Amount'[BY SUM: Sales.Product]"],"kwargs":{"kind":"number","overridable":false}}`,
		`{"op":"add_input","args":["Owner",["Product"],[]],"kwargs":{"kind":"member:Region"}}`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %s in %s", want, got)
		}
	}
}

func TestModelDefHidesMembersAndPropertyMetrics(t *testing.T) {
	em := model(t)
	meta := appMeta{Props: []propRow{{List: "Sales", Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER}}}
	cells := map[string]engineCube{"Sales.Amount": {Dims: []string{"Sales"}, Cells: [][]any{{"1", 10.5}}}}
	lists, metrics := modelDef(em, meta, cells, eastOnly())
	if r := lists[2]; len(r.Members) != 1 || r.Members[0].Name != "East" {
		t.Errorf("Region: %v", r.Members)
	}
	if s := lists[1].Members[0].Properties; s["Amount"] != "10.5" || s["Product"] != "A" {
		t.Errorf("Sales 1: %v", s)
	}
	for _, m := range metrics {
		if m.Name == "Sales.Amount" {
			t.Error("a property Metric must not be in the Metrics")
		}
	}
}

func TestMetricOpKeepsInputCells(t *testing.T) {
	em := model(t)
	budget := func(dims ...string) *nanashiv1.MetricDef {
		return &nanashiv1.MetricDef{Name: "Budget", Dimensions: dims}
	}
	if ops, err := metricOp(em, budget("Product", "Region"), false); err != nil || len(ops) != 0 {
		t.Errorf("same input Metric: got %v, %v, want no operation", ops, err)
	}
	for _, m := range []*nanashiv1.MetricDef{
		budget("Product"),
		{Name: "Budget", Dimensions: []string{"Product", "Region"}, Kind: "boolean"},
		{Name: "Budget", Dimensions: []string{"Product", "Region"}, Formula: "1"},
	} {
		if _, err := metricOp(em, m, false); err == nil {
			t.Errorf("%v: replaced the input cells without replace", m)
		}
		if ops, err := metricOp(em, m, true); err != nil || len(ops) != 1 {
			t.Errorf("%v with replace: got %v, %v", m, ops, err)
		}
	}
	if ops, err := metricOp(em, &nanashiv1.MetricDef{Name: "Revenue", Dimensions: []string{"Product"}, Formula: "2"}, false); err != nil ||
		opsJSON(t, ops) != `[{"op":"add_formula","args":["Revenue",["Product"],"2"],"kwargs":{"kind":"number","overridable":false}}]` {
		t.Errorf("formula change: got %s, %v", opsJSON(t, ops), err)
	}
}

func TestVisibleComments(t *testing.T) {
	l := eastOnly().through(model(t))
	comments := []*nanashiv1.Comment{
		{Id: "1", Cell: map[string]string{"Region": "East", "Product": "A"}},
		{Id: "2", Cell: map[string]string{"Region": "West"}},
		{Id: "3"},
	}
	var ids []string
	for _, c := range visibleComments(comments, l) {
		ids = append(ids, c.Id)
	}
	if got := strings.Join(ids, ","); got != "1,3" {
		t.Errorf("visible comments: %s, want 1,3", got)
	}
}
