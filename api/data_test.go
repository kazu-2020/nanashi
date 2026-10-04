package api

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

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
		if _, err := writeOps([]*nanashiv1.CellWrite{{Metric: "Budget", Coords: coords, Value: num(1)}}, em, eastOnly()); !errors.Is(err, errDenied) {
			t.Errorf("%s: got %v, want a permission error", name, err)
		}
	}
	if _, err := writeOps([]*nanashiv1.CellWrite{{Metric: "Nothing", Value: num(1)}}, em, eastOnly()); err == nil || errors.Is(err, errDenied) {
		t.Errorf("unknown Metric: got %v, want an input error", err)
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
	// Revenue is a formula without Region, so it can show West data. Owner is an input without Region.
	want := `[{"Metric":"Budget","Path":"summary","Query":{"Product":["A"],"Region":["East"],"agg":["sum"],"keep":["Product,Region"]}},` +
		`{"Metric":"Owner","Path":"slice","Query":{"Product":["A"]}}]`
	if string(got) != want {
		t.Errorf("got %s", got)
	}
	// A filter outside the rule leaves no member, so the Metric is left out.
	req.Filters["Region"] = &nanashiv1.Members{Names: []string{"West"}}
	if reads, _ := queryReads(req, em, eastOnly()); len(reads) != 1 || reads[0].Metric != "Owner" {
		t.Errorf("got %+v", reads)
	}
	// A rule that gives all members sends no filter: the names of a big list do not fit in the URL.
	all := accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: "Region", Members: []string{"East", "West"}}})
	req.Filters = nil
	if reads, _ := queryReads(req, em, all); reads[0].Query["Region"] != nil {
		t.Errorf("got %+v", reads[0].Query)
	}
	// A removed member in a filter is dropped, not sent to the engine.
	req.Filters = map[string]*nanashiv1.Members{"Product": {Names: []string{"A", "Gone"}}}
	if reads, _ := queryReads(req, em, nil); reads[0].Query["Product"][0] != "A" {
		t.Errorf("got %+v", reads)
	}
}

func TestQueryCells(t *testing.T) {
	cube := engineCube{Dims: []string{"Product"}, Cells: [][]any{{"A", 11.0}, {"B", nil}}}
	revenue, _ := model(t).metric("Revenue")
	cells := queryCells(revenue, []string{"Region", "Product"}, cube, nil)
	if len(cells) != 1 || strings.Join(cells[0].Coords, ",") != ",A" || cells[0].Value.GetNumber() != 11 {
		t.Errorf("got %v", cells)
	}
}

func TestQueryCellsHidesMemberValues(t *testing.T) {
	owner, _ := model(t).metric("Owner") // member:Region
	cube := engineCube{Dims: []string{"Product"}, Cells: [][]any{{"A", "West"}, {"B", "East"}}}
	cells := queryCells(owner, []string{"Product"}, cube, eastOnly())
	if len(cells) != 1 || cells[0].Coords[0] != "B" || cells[0].Value.GetMember() != "East" {
		t.Errorf("a reader limited to East must not see West as a value: %v", cells)
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
