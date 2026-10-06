package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

const newMember = "0192f3a4-0000-7000-8000-0000000000aa"

func TestEditMembersRenameRemove(t *testing.T) {
	em := model(t)
	dim, _ := em.dim(sales)
	p, err := editOps("app", em, dim, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale1, Properties: map[string]string{salesProduct: memberA}}}},
		{Edit: &nanashiv1.MemberEdit_Rename{Rename: &nanashiv1.RenameMember{Id: sale1, Name: " one "}}},
		{Edit: &nanashiv1.MemberEdit_Remove{Remove: &nanashiv1.RemoveMember{Id: sale2}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale9, Properties: map[string]string{salesProduct: memberB}}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale1, Properties: map[string]string{salesProduct: " "}}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	// A rename changes no reference: the values of a member go at the end, in one operation. A blank value removes the value.
	want := fmt.Sprintf(`[{"dim":%q,"id":%q,"name":"one","op":"rename_member"},{"dim":%q,"id":%q,"op":"remove_member"},`+
		`{"dim":%q,"op":"set_property_values","prop":%q,"values":{%q:null,%q:%q}}]`, sales, sale1, sales, sale2, sales, salesProduct, sale1, sale9, memberB)
	if got := opsJSON(t, p.ops); got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	if len(p.stmts) != 0 {
		t.Errorf("a rename or a removal must change no api row: %v", p.stmts)
	}
	if _, err := editOps("app", em, dim, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale1, Properties: map[string]string{newMember: "x"}}}},
	}); err == nil {
		t.Error("an unknown property must be an error")
	}
	if _, err := editOps("app", em, dim, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale1, Properties: map[string]string{salesProduct: "A"}}}},
	}); err == nil {
		t.Error("a member name as the value of a DIMENSION property must be an error")
	}
}

func TestEditMembersTextAndNumber(t *testing.T) {
	em := model(t)
	dim, _ := em.dim(sales)
	note, amountProp := "0192f3a4-0000-7000-8000-0000000000b1", "0192f3a4-0000-7000-8000-0000000000b2"
	meta := appMeta{Props: []propRow{
		{ListID: sales, ID: amountProp, Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, MetricID: amount},
		{ListID: sales, ID: note, Name: "Note", Type: nanashiv1.PropertyType_PROPERTY_TYPE_TEXT},
	}}
	p, err := editOps("app", em, dim, meta, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Add{Add: &nanashiv1.AddMember{Id: newMember, Name: "10", Properties: map[string]string{amountProp: "1,200", note: "first"}}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale1, Properties: map[string]string{note: ""}}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf(`[{"dim":%q,"id":%q,"name":"10","op":"add_member"},{"coords":{%q:%q},"metric":%q,"op":"set_cell","value":1200}]`, sales, newMember, sales, newMember, amount)
	if got := opsJSON(t, p.ops); got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	if len(p.stmts) != 1 || fmt.Sprint(p.stmts[0].args) != fmt.Sprintf(`[app %s %s [%s] {%q:"first"}]`, sales, note, sale1, newMember) {
		t.Errorf("text statements: %v", p.stmts)
	}
}

func TestCalendarOps(t *testing.T) {
	cal, err := calendarOps(2026, 2)
	if err != nil {
		t.Fatal(err)
	}
	var dims, members, props, values int
	for _, o := range cal.ops {
		switch o["op"] {
		case "add_dimension":
			dims++
			if o["ordered"] != true {
				t.Error("a calendar list is ordered")
			}
		case "add_member":
			members++
		case "add_property":
			props++
		case "set_property_values":
			values++
		}
	}
	if dims != 3 || members != 2+8+24 || props != 3 || values != 3 || len(cal.lists) != 3 {
		t.Errorf("got %d dims, %d members, %d props, %d values", dims, members, props, values)
	}
	last := cal.ops[len(cal.ops)-1]["values"].(map[string]*string)
	if len(last) != 8 { // Quarter.Year has 8 quarters.
		t.Errorf("Quarter.Year has %d values, want 8", len(last))
	}
	if _, err := calendarOps(2026, 0); err == nil {
		t.Error("0 years must be an error")
	}
}

func TestModelDefHidesPropertyMetrics(t *testing.T) {
	em := model(t)
	peer := "0192f3a4-0000-7000-8000-0000000000c1"
	em.Dims[2].Props = []engineProp{{ID: peer, Name: "Peer", Target: region, Values: map[string]string{east: west}}}
	amountProp, text := "0192f3a4-0000-7000-8000-0000000000c2", "0192f3a4-0000-7000-8000-0000000000c3"
	meta := appMeta{Props: []propRow{
		{ListID: sales, ID: amountProp, Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, MetricID: amount},
		{ListID: region, ID: peer, Name: "Peer", Type: nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION},
		{ListID: region, ID: text, Name: "Gone", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, MetricID: newMember}, // Its Metric is pending.
		{ListID: sales, ID: salesProduct, Name: "Product", Type: nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION},
	}}
	cells := map[string]engineCube{amount: {Dims: []string{sales}, Cells: [][]any{{sale1, 10.5}}}}
	lists, metrics := modelDef(em, meta, cells)
	if r := lists[2]; len(r.Members) != 2 || r.Members[0].Id != east || r.Members[0].Name != "East" || r.Members[0].Properties[peer] != west || len(r.Properties) != 1 {
		t.Errorf("Region: %v", r)
	}
	if s := lists[1].Members[0].Properties; s[amountProp] != "10.5" || s[salesProduct] != memberA {
		t.Errorf("Sales 1: %v", s)
	}
	if p := lists[1].Properties; len(p) != 2 || p[1].Target != product {
		t.Errorf("Sales properties: %v", p)
	}
	for _, m := range metrics {
		if m.Id == amount {
			t.Error("a property Metric must not be in the Metrics")
		}
	}
}

func TestEditMembersSelfReference(t *testing.T) {
	parent := "0192f3a4-0000-7000-8000-0000000000d1"
	x, a, b, c := "0192f3a4-0000-7000-8000-0000000000d2", "0192f3a4-0000-7000-8000-0000000000d3", "0192f3a4-0000-7000-8000-0000000000d4", "0192f3a4-0000-7000-8000-0000000000d5"
	em := engineModel{Dims: []engineDim{{ID: product, Name: "Product", Members: []engineMember{{x, "X"}},
		Props: []engineProp{{ID: parent, Name: "Parent", Target: product, Values: map[string]string{}}}}}}
	add := func(id, name, parentID string) *nanashiv1.MemberEdit {
		return &nanashiv1.MemberEdit{Edit: &nanashiv1.MemberEdit_Add{Add: &nanashiv1.AddMember{Id: id, Name: name, Properties: map[string]string{parent: parentID}}}}
	}
	p, err := editOps("app", em, em.Dims[0], appMeta{}, []*nanashiv1.MemberEdit{add(a, "A", x), add(c, "C", x), add(b, "B", "")})
	if err != nil {
		t.Fatal(err)
	}
	got := opsJSON(t, p.ops)
	if !strings.HasSuffix(got, fmt.Sprintf(`{"dim":%q,"op":"set_property_values","prop":%q,"values":{%q:%q,%q:null,%q:%q}}]`, product, parent, a, x, b, c, x)) {
		t.Errorf("got %s", got)
	}
	if _, err := editOps("app", em, em.Dims[0], appMeta{}, []*nanashiv1.MemberEdit{add(a, "A", b)}); err == nil {
		t.Error("a value that is not a member must be an error")
	}
}

func TestPruneItems(t *testing.T) {
	em := model(t)
	view := "0192f3a4-0000-7000-8000-0000000000e1"
	out := &nanashiv1.ModelDef{
		Tables: []*nanashiv1.TableDef{{Metrics: []string{budget, newMember}}},
		Views:  []*nanashiv1.ViewDef{{Id: view, Metrics: []string{newMember}, Rows: []string{product, newMember}}},
		Boards: []*nanashiv1.BoardDef{{PageSelectors: []string{region, newMember}, Widgets: []*nanashiv1.Widget{
			{Content: &nanashiv1.Widget_ViewId{ViewId: view}}, {Content: &nanashiv1.Widget_ViewId{ViewId: newMember}}, {Content: &nanashiv1.Widget_Text{Text: "t"}}}}},
	}
	pruneItems(out, em)
	if fmt.Sprint(out.Tables[0].Metrics, out.Views[0].Metrics, out.Views[0].Rows, out.Boards[0].PageSelectors) != fmt.Sprint([]string{budget}, []string{}, []string{product}, []string{region}) {
		t.Errorf("got %v", out)
	}
	if len(out.Boards[0].Widgets) != 2 {
		t.Errorf("widgets: %v", out.Boards[0].Widgets)
	}
}

// TestCreateScenarioReplansWhenTheCubesAreNewer: the copy reads the cells after the model. If a write came in
// between, the cubes have another seq than the model: the plan runs again with the new model and cells.
func TestCreateScenarioReplansWhenTheCubesAreNewer(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	if _, err := pool.Exec(ctx, "insert into app_list (app_id, id, kind) values ($1, $2, $3)", app, product, nanashiv1.ListKind_LIST_KIND_SCENARIO); err != nil {
		t.Fatal(err)
	}
	e := newFakeEngine(t, sample)
	e.server.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		e.mu.Lock()
		defer e.mu.Unlock()
		switch {
		case r.Method == "POST":
			var body map[string]any
			b, _ := io.ReadAll(r.Body)
			json.Unmarshal(b, &body)
			e.writes = append(e.writes, body)
			io.WriteString(w, `{"seq": 9}`)
		case strings.HasSuffix(r.URL.Path, "/"):
			e.gets++
			// The first model read is older than the cells (a write came in between).
			io.WriteString(w, strings.Replace(sample, `"seq": 7`, fmt.Sprintf(`"seq": %d`, 6+e.gets), 1))
		default:
			fmt.Fprintf(w, `{"seq": 8, "dims": [%q, %q], "cells": [[%q, %q, %d]]}`, product, region, memberA, east, e.gets)
		}
	})
	alice := testClient(t, pool, e)("alice")
	_, err := alice.CreateScenario(ctx, connect.NewRequest(&nanashiv1.CreateScenarioRequest{AppId: app, ClientOpId: uuid.NewString(), Id: uuid.Must(uuid.NewV7()).String(), Name: "S2", CopyFrom: memberA}))
	if err != nil {
		t.Fatal(err)
	}
	if e.gets != 2 {
		t.Errorf("model reads: %d, want 2 (one plan again after the cells were newer)", e.gets)
	}
	if len(e.writes) != 1 {
		t.Fatalf("engine writes: %d, want 1", len(e.writes))
	}
	for _, o := range e.writes[0]["ops"].([]any) {
		if o := o.(map[string]any); o["op"] == "set_cell" && o["value"] != 2.0 {
			t.Errorf("the copy must have the cells of the second read: %v", o)
		}
	}
	if _, ok := e.writes[0]["expect"]; ok {
		t.Errorf("expect is of no use for a copy: nobody else writes the new scenario")
	}
}

// TestEditMembersRefusalRestoresText: the TEXT values of a refused edit go back to the old values.
func TestEditMembersRefusalRestoresText(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	note := uuid.Must(uuid.NewV7()).String()
	if _, err := pool.Exec(ctx, "insert into app_property (app_id, list_id, id, name, type, text_values) values ($1, $2, $3, 'Note', $4, $5)",
		app, product, note, nanashiv1.PropertyType_PROPERTY_TYPE_TEXT, fmt.Sprintf(`{%q: "a", %q: "b"}`, memberA, memberB)); err != nil {
		t.Fatal(err)
	}
	e := newFakeEngine(t, sample)
	e.reply = func(int, map[string]any) (int, string) { return 400, `{"error": "bad_request", "message": "だめ"}` }
	alice := testClient(t, pool, e)("alice")
	_, err := alice.EditMembers(ctx, connect.NewRequest(&nanashiv1.EditMembersRequest{AppId: app, ClientOpId: uuid.NewString(), List: product, Edits: []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: memberA, Properties: map[string]string{note: "x"}}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: memberB, Properties: map[string]string{note: ""}}}},
		{Edit: &nanashiv1.MemberEdit_Remove{Remove: &nanashiv1.RemoveMember{Id: memberB}}},
	}}))
	if connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("got %v, want the refusal", err)
	}
	var text string
	if err := pool.QueryRow(ctx, "select text_values::text from app_property where app_id = $1 and id = $2", app, note).Scan(&text); err != nil {
		t.Fatal(err)
	}
	if want := fmt.Sprintf(`{%q: "a", %q: "b"}`, memberA, memberB); text != want {
		t.Errorf("text values after the refusal: %s, want %s", text, want)
	}
}

func TestRenameListRenamesThePropertyMetrics(t *testing.T) {
	em := model(t)
	amountProp, text := "0192f3a4-0000-7000-8000-0000000000d1", "0192f3a4-0000-7000-8000-0000000000d2"
	meta := appMeta{Props: []propRow{
		{ListID: sales, ID: amountProp, Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, MetricID: amount},
		{ListID: sales, ID: text, Name: "Gone", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, MetricID: newMember}, // Its Metric is pending.
		{ListID: sales, ID: salesProduct, Name: "Product", Type: nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION},
	}}
	p, err := renameListPlan(em, meta, sales, "Orders")
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf(`[{"id":%q,"name":"Orders","op":"rename_dimension"},{"id":%q,"name":"Orders.Amount","op":"rename_metric"}]`, sales, amount)
	if got := opsJSON(t, p.ops); got != want || len(p.stmts) != 0 {
		t.Errorf("got %s %v\nwant %s and no statement", got, p.stmts, want)
	}
	if _, err := renameListPlan(em, meta, newMember, "X"); connect.CodeOf(connectError(err)) != connect.CodeNotFound {
		t.Errorf("an unknown list: got %v, want NOT_FOUND", err)
	}
}

func TestRenamePropertyPlan(t *testing.T) {
	em := model(t)
	amountProp, note := "0192f3a4-0000-7000-8000-0000000000e1", "0192f3a4-0000-7000-8000-0000000000e2"
	meta := appMeta{Props: []propRow{
		{ListID: sales, ID: amountProp, Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, MetricID: amount},
		{ListID: sales, ID: salesProduct, Name: "Product", Type: nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION},
		{ListID: sales, ID: note, Name: "Note", Type: nanashiv1.PropertyType_PROPERTY_TYPE_TEXT},
	}}
	for id, want := range map[string]string{
		amountProp:   fmt.Sprintf(`[{"id":%q,"name":"Sales.New","op":"rename_metric"}]`, amount),
		salesProduct: fmt.Sprintf(`[{"dim":%q,"id":%q,"name":"New","op":"rename_property"}]`, sales, salesProduct),
		note:         `null`,
	} {
		p, err := renamePropertyPlan("app", em, meta, sales, id, "New")
		if err != nil {
			t.Fatal(err)
		}
		if got := opsJSON(t, p.ops); got != want {
			t.Errorf("%s: got %s, want %s", id, got, want)
		}
		if len(p.stmts) != 1 || len(p.made) != 1 || p.made[0].NewName != "New" {
			t.Errorf("%s: the api row must get the new name: %v %v", id, p.stmts, p.made)
		}
	}
	if _, err := renamePropertyPlan("app", em, meta, sales, note, "Amount"); connect.CodeOf(connectError(err)) != connect.CodeAlreadyExists {
		t.Errorf("a name of another property: got %v, want ALREADY_EXISTS", err)
	}
	if _, err := renamePropertyPlan("app", em, meta, product, note, "X"); connect.CodeOf(connectError(err)) != connect.CodeNotFound {
		t.Errorf("a property of another list: got %v, want NOT_FOUND", err)
	}
}

// TestRenamePropertyRefusalRestoresName: the engine refuses the new name, so the api row gets the old name back.
func TestRenamePropertyRefusalRestoresName(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	if _, err := pool.Exec(ctx, "insert into app_property (app_id, list_id, id, name, type) values ($1, $2, $3, 'Product', $4)",
		app, sales, salesProduct, nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION); err != nil {
		t.Fatal(err)
	}
	e := newFakeEngine(t, sample)
	alice := testClient(t, pool, e)("alice")
	rename := func(name string) error {
		_, err := alice.RenameProperty(ctx, connect.NewRequest(&nanashiv1.RenamePropertyRequest{AppId: app, ClientOpId: uuid.NewString(), List: sales, Id: salesProduct, Name: name}))
		return err
	}
	nameOf := func() (name string) {
		if err := pool.QueryRow(ctx, "select name from app_property where app_id = $1 and id = $2", app, salesProduct).Scan(&name); err != nil {
			t.Fatal(err)
		}
		return name
	}
	if err := rename(" Item "); err != nil || nameOf() != "Item" {
		t.Fatalf("got %v and %q, want the name Item", err, nameOf())
	}
	if w := e.writes; len(w) != 1 || w[0]["ops"].([]any)[0].(map[string]any)["op"] != "rename_property" {
		t.Errorf("engine writes: %v", w)
	}
	e.reply = func(int, map[string]any) (int, string) { return 400, `{"error": "bad_request", "message": "だめ"}` }
	if err := rename("Taken"); connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("got %v, want the refusal", err)
	}
	if got := nameOf(); got != "Item" {
		t.Errorf("name after the refusal: %q, want Item", got)
	}
}
