package api

import (
	"fmt"
	"strings"
	"testing"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

const newMember = "0192f3a4-0000-7000-8000-0000000000aa"

func TestEditMembersRenameRemove(t *testing.T) {
	em := model(t)
	dim, _ := em.dim(sales)
	ops, stmts, err := editOps("app", em, dim, appMeta{}, []*nanashiv1.MemberEdit{
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
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	if len(stmts) != 0 {
		t.Errorf("a rename or a removal must change no api row: %v", stmts)
	}
	if _, _, err := editOps("app", em, dim, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale1, Properties: map[string]string{newMember: "x"}}}},
	}); err == nil {
		t.Error("an unknown property must be an error")
	}
	if _, _, err := editOps("app", em, dim, appMeta{}, []*nanashiv1.MemberEdit{
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
	ops, stmts, err := editOps("app", em, dim, meta, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Add{Add: &nanashiv1.AddMember{Id: newMember, Name: "10", Properties: map[string]string{amountProp: "1,200", note: "first"}}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: sale1, Properties: map[string]string{note: ""}}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf(`[{"dim":%q,"id":%q,"name":"10","op":"add_member"},{"coords":{%q:%q},"metric":%q,"op":"set_cell","value":1200}]`, sales, newMember, sales, newMember, amount)
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	if len(stmts) != 1 || fmt.Sprint(stmts[0].args) != fmt.Sprintf(`[app %s %s [%s] {%q:"first"}]`, sales, note, sale1, newMember) {
		t.Errorf("text statements: %v", stmts)
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

func TestModelDefHidesMembersAndPropertyMetrics(t *testing.T) {
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
	lists, metrics := modelDef(em, meta, cells, eastOnly())
	if r := lists[2]; len(r.Members) != 1 || r.Members[0].Id != east || r.Members[0].Name != "East" || r.Members[0].Properties[peer] != "" || len(r.Properties) != 1 {
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
		if m.Id == revenue {
			t.Error("a formula Metric without the limited list Region must be hidden")
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
	ops, _, err := editOps("app", em, em.Dims[0], appMeta{}, []*nanashiv1.MemberEdit{add(a, "A", x), add(c, "C", x), add(b, "B", "")})
	if err != nil {
		t.Fatal(err)
	}
	got := opsJSON(t, ops)
	if !strings.HasSuffix(got, fmt.Sprintf(`{"dim":%q,"op":"set_property_values","prop":%q,"values":{%q:%q,%q:null,%q:%q}}]`, product, parent, a, x, b, c, x)) {
		t.Errorf("got %s", got)
	}
	if _, _, err := editOps("app", em, em.Dims[0], appMeta{}, []*nanashiv1.MemberEdit{add(a, "A", b)}); err == nil {
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
