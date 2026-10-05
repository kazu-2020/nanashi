package api

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestImportTransactionList(t *testing.T) {
	em := model(t)
	dim, _ := em.dim(sales)
	amountProp, note := "0192f3a4-0000-7000-8000-0000000000b2", "0192f3a4-0000-7000-8000-0000000000b1"
	meta := appMeta{Kinds: map[string]nanashiv1.ListKind{sales: nanashiv1.ListKind_LIST_KIND_TRANSACTION}, Props: []propRow{
		{ListID: sales, ID: amountProp, Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, MetricID: amount},
		{ListID: sales, ID: note, Name: "Note", Type: nanashiv1.PropertyType_PROPERTY_TYPE_TEXT, Text: map[string]string{sale1: "old"}},
	}}
	csv := "product,amount,note\nA,\"1,200\",first\nB,5,\n"
	edits, rows, err := importListEdits(csv, &nanashiv1.ListImport{List: sales,
		PropertyColumns: map[string]string{salesProduct: "product", amountProp: "amount", note: "note"}}, em, dim, meta, 10)
	if err != nil || rows != 2 {
		t.Fatal(rows, err)
	}
	p, err := editOps("app", em, dim, meta, edits)
	if err != nil {
		t.Fatal(err)
	}
	// New rows start at first (10) with new ids. The DIMENSION column has names, the edit has ids.
	// One operation and one statement set the values of all rows.
	id10, id11 := edits[0].GetAdd().Id, edits[1].GetAdd().Id
	if _, err := parseID("id", id10); err != nil || id10 == id11 {
		t.Fatalf("ids of the new members: %s, %s", id10, id11)
	}
	want := fmt.Sprintf(`[{"dim":%q,"id":%q,"name":"10","op":"add_member"},{"coords":{%q:%q},"metric":%q,"op":"set_cell","value":1200},`+
		`{"dim":%q,"id":%q,"name":"11","op":"add_member"},{"coords":{%q:%q},"metric":%q,"op":"set_cell","value":5},`+
		`{"dim":%q,"op":"set_property_values","prop":%q,"values":{%q:%q,%q:%q}}]`,
		sales, id10, sales, id10, amount, sales, id11, sales, id11, amount, sales, salesProduct, id10, memberA, id11, memberB)
	if got := opsJSON(t, p.ops); got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	// Row 10 sets its Note, and the blank Note of row 11 removes the value. The other values stay in the table.
	if len(p.stmts) != 1 || fmt.Sprint(p.stmts[0].args) != fmt.Sprintf(`[app %s %s [%s] {%q:"first"}]`, sales, note, id11, id10) {
		t.Errorf("text statements: %v", p.stmts)
	}
	if _, _, err := importListEdits("product\nNope\n", &nanashiv1.ListImport{List: sales, PropertyColumns: map[string]string{salesProduct: "product"}}, em, dim, meta, 1); err == nil {
		t.Error("an unknown member name in a DIMENSION column must be an error")
	}
}

// TestImportSelfReference: a property of a list can name a member that the same import adds, in any row.
func TestImportSelfReference(t *testing.T) {
	emp, mgr := "0192f3a4-0000-7000-8000-0000000000c1", "0192f3a4-0000-7000-8000-0000000000c2"
	dim := engineDim{ID: emp, Name: "Employee", Props: []engineProp{{ID: mgr, Name: "Manager", Target: emp}}}
	em := engineModel{Dims: []engineDim{dim}}
	edits, _, err := importListEdits("name,manager\nBob,Alice\nAlice,\n", &nanashiv1.ListImport{List: emp, MemberColumn: "name",
		PropertyColumns: map[string]string{mgr: "manager"}}, em, dim, appMeta{}, 1)
	if err != nil {
		t.Fatal(err)
	}
	bob, alice := edits[0].GetAdd(), edits[1].GetAdd()
	if bob.Name != "Bob" || alice.Name != "Alice" || bob.Properties[mgr] != alice.Id {
		t.Errorf("Bob.Manager = %q, want the id of Alice %q", bob.Properties[mgr], alice.Id)
	}
	// The engine gets the property values after it adds both members.
	p, err := editOps("app", em, dim, appMeta{}, edits)
	want := fmt.Sprintf(`[{"dim":%q,"id":%q,"name":"Bob","op":"add_member"},{"dim":%q,"id":%q,"name":"Alice","op":"add_member"},`+
		`{"dim":%q,"op":"set_property_values","prop":%q,"values":{%q:%q,%q:null}}]`, emp, bob.Id, emp, alice.Id, emp, mgr, bob.Id, alice.Id, alice.Id)
	if err != nil || opsJSON(t, p.ops) != want {
		t.Errorf("editOps: got %s, %v\nwant %s", opsJSON(t, p.ops), err, want)
	}
}

func TestLargestRow(t *testing.T) {
	em := model(t)
	dim, _ := em.dim(sales)
	if got := largestRow(dim); got != 9 {
		t.Errorf("largestRow = %d, want 9", got)
	}
	if got := largestRow(engineDim{}); got != 0 {
		t.Errorf("largestRow of an empty list = %d, want 0", got)
	}
}

// TestConcurrentImportsGetDifferentRows: concurrent imports into a TRANSACTION list read the same model, so the
// row numbers come from the counter app_list.next_row, not from the model.
func TestConcurrentImportsGetDifferentRows(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	if _, err := pool.Exec(ctx, "insert into app_list (app_id, id, kind) values ($1, $2, $3)", app, sales, nanashiv1.ListKind_LIST_KIND_TRANSACTION); err != nil {
		t.Fatal(err)
	}
	e := newFakeEngine(t, sample)
	alice := testClient(t, pool, e)("alice")
	var wg sync.WaitGroup
	errs := make([]error, 8)
	for i := range errs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, errs[i] = alice.Import(ctx, connect.NewRequest(&nanashiv1.ImportRequest{AppId: app, ClientOpId: uuid.NewString(), Csv: "amount\n1\n",
				Target: &nanashiv1.ImportRequest_List{List: &nanashiv1.ListImport{List: sales}}}))
		}()
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	names := map[string]bool{}
	e.mu.Lock()
	for _, w := range e.writes {
		for _, o := range w["ops"].([]any) {
			if o := o.(map[string]any); o["op"] == "add_member" {
				names[o["name"].(string)] = true
			}
		}
	}
	e.mu.Unlock()
	if len(names) != 8 || !names["10"] || !names["17"] {
		t.Errorf("row names: got %v, want 10 to 17", slices.Sorted(maps.Keys(names)))
	}
}

func TestImportMetric(t *testing.T) {
	em := model(t)
	imp := &nanashiv1.MetricImport{Metric: budget, DimensionColumns: map[string]string{product: "p", region: "r"}, ValueColumn: "v"}
	csv := "p,r,v\nA,East,3\nC,East,4\n"
	if _, _, err := importMetricOps(csv, imp, em, limits{}); err == nil || !strings.Contains(err.Error(), "3 行目") {
		t.Errorf("unknown member: got %v", err)
	}
	imp.AddMembers = true
	ops, rows, err := importMetricOps(csv, imp, em, limits{})
	if err != nil || rows != 2 {
		t.Fatal(rows, err)
	}
	c := ops[0]["id"].(string)
	want := fmt.Sprintf(`[{"dim":%q,"id":%q,"name":"C","op":"add_member"},{"coords":{%q:%q,%q:%q},"metric":%q,"op":"set_cell","value":3},`+
		`{"coords":{%q:%q,%q:%q},"metric":%q,"op":"set_cell","value":4}]`, product, c, product, memberA, region, east, budget, product, c, region, east, budget)
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	if _, _, err := importMetricOps("p,r,v\nA,West,1\n", imp, em, eastOnly()); !errors.Is(err, errDenied) {
		t.Errorf("a row outside the access rule: got %v, want a permission error", err)
	}
}
