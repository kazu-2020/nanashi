package api

import (
	"fmt"
	"testing"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestEditMembersRenameRemove(t *testing.T) {
	em := model(t)
	ops, stmts, err := editOps("app", "Sales", em, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Name: "1", Properties: map[string]string{"Product": "A"}}}},
		{Edit: &nanashiv1.MemberEdit_Rename{Rename: &nanashiv1.RenameMember{Name: "1", NewName: " one "}}},
		{Edit: &nanashiv1.MemberEdit_Remove{Remove: &nanashiv1.RemoveMember{Name: "2"}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Name: "9", Properties: map[string]string{"Product": "B"}}}},
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Name: "one", Properties: map[string]string{"Product": " "}}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	// The values of a member go to the engine before its rename, and the engine follows the rename and the removal.
	// A blank value removes the value.
	want := `[{"op":"set_property_values","args":["Sales","Product",{"1":"A"}]},` +
		`{"op":"rename_member","args":["Sales","1","one"]},{"op":"remove_member","args":["Sales","2"]},` +
		`{"op":"set_property_values","args":["Sales","Product",{"9":"B","one":null}]}]`
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s", got)
	}
	// The api tables get the trimmed new name once for each reference site, and nil for the removed member.
	if len(stmts) != 2*len(memberRefs) || fmt.Sprint(stmts[0].args[:3]) != "[app Sales 1]" || *stmts[0].args[3].(*string) != "one" ||
		fmt.Sprint(stmts[len(memberRefs)].args) != "[app Sales 2 <nil>]" {
		t.Errorf("reference statements: %v", stmts)
	}
	if _, _, err := editOps("app", "Sales", em, appMeta{}, []*nanashiv1.MemberEdit{
		{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Name: "1", Properties: map[string]string{"Color": "x"}}}},
	}); err == nil {
		t.Error("an unknown property must be an error")
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

func TestModelDefHidesMembersAndPropertyMetrics(t *testing.T) {
	em := model(t)
	em.Dims[2].Props = []engineProp{{Name: "Peer", Target: "Region", Values: map[string]string{"East": "West"}}}
	meta := appMeta{Props: []propRow{{List: "Sales", Name: "Amount", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER}}}
	cells := map[string]engineCube{"Sales.Amount": {Dims: []string{"Sales"}, Cells: [][]any{{"1", 10.5}}}}
	lists, metrics := modelDef(em, meta, cells, eastOnly())
	if r := lists[2]; len(r.Members) != 1 || r.Members[0].Name != "East" || r.Members[0].Properties["Peer"] != "" {
		t.Errorf("Region: %v", r.Members)
	}
	if s := lists[1].Members[0].Properties; s["Amount"] != "10.5" || s["Product"] != "A" {
		t.Errorf("Sales 1: %v", s)
	}
	for _, m := range metrics {
		if m.Name == "Sales.Amount" {
			t.Error("a property Metric must not be in the Metrics")
		}
		if m.Name == "Revenue" {
			t.Error("a formula Metric without the limited list Region must be hidden")
		}
	}
}
