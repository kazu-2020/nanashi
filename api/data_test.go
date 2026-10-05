package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestWriteOps(t *testing.T) {
	em := model(t)
	num := func(v float64) *nanashiv1.Value { return &nanashiv1.Value{Value: &nanashiv1.Value_Number{Number: v}} }
	ops, err := writeOps([]*nanashiv1.CellWrite{
		{Metric: budget, Coords: map[string]string{product: memberA, region: east}, Value: num(5)},
		{Metric: budget, Coords: map[string]string{region: east}, Value: num(100)},
		{Metric: budget, Coords: map[string]string{product: memberB, region: east}},
	}, em, eastOnly())
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf(`[{"coords":{%q:%q,%q:%q},"metric":%q,"op":"set_cell","value":5},`+
		`{"coords":{%q:%q},"metric":%q,"op":"spread","total":100},`+
		`{"coords":{%q:%q,%q:%q},"metric":%q,"op":"set_cell","value":null}]`,
		product, memberA, region, east, budget, region, east, budget, product, memberB, region, east, budget)
	if got := opsJSON(t, ops); got != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	for name, coords := range map[string]map[string]string{
		"outside the rule":  {product: memberA, region: west},
		"spread over rules": {product: memberA},
	} {
		if _, err := writeOps([]*nanashiv1.CellWrite{{Metric: budget, Coords: coords, Value: num(1)}}, em, eastOnly()); !errors.Is(err, errDenied) {
			t.Errorf("%s: got %v, want a permission error", name, err)
		}
	}
	if _, err := writeOps([]*nanashiv1.CellWrite{{Metric: newMember, Value: num(1)}}, em, eastOnly()); err == nil || errors.Is(err, errDenied) {
		t.Errorf("unknown Metric: got %v, want an input error", err)
	}
}

func TestQueryReads(t *testing.T) {
	em := model(t)
	req := &nanashiv1.QueryRequest{Metrics: []string{budget, revenue, own, newMember}, Rows: []string{product}, Columns: []string{region},
		Filters: map[string]*nanashiv1.Members{product: {Ids: []string{memberA}}}}
	reads, err := queryReads(req, em, eastOnly())
	if err != nil {
		t.Fatal(err)
	}
	got, _ := json.Marshal(reads)
	// Revenue is a formula without Region, so it can show West data. Owner is an input without Region. An unknown Metric is skipped.
	want := fmt.Sprintf(`[{"Metric":%q,"Path":"summary","Query":{%q:[%q],%q:[%q],"agg":["sum"],"keep":["%s,%s"]}},`+
		`{"Metric":%q,"Path":"slice","Query":{%q:[%q]}}]`, budget, product, memberA, region, east, product, region, own, product, memberA)
	if string(got) != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	// A filter outside the rule leaves no member, so the Metric is left out.
	req.Filters[region] = &nanashiv1.Members{Ids: []string{west}}
	if reads, _ := queryReads(req, em, eastOnly()); len(reads) != 1 || reads[0].Metric != own {
		t.Errorf("got %+v", reads)
	}
	// A rule that gives all members sends no filter: the ids of a big list do not fit in the URL.
	all := accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: region, Members: []string{east, west}}}, em)
	req.Filters = nil
	if reads, _ := queryReads(req, em, all); reads[0].Query[region] != nil {
		t.Errorf("got %+v", reads[0].Query)
	}
	// A removed member in a filter is dropped, not sent to the engine. A filter of removed members only gives nothing.
	req.Filters = map[string]*nanashiv1.Members{product: {Ids: []string{memberA, newMember}}}
	if reads, _ := queryReads(req, em, nil); reads[0].Query[product][0] != memberA {
		t.Errorf("got %+v", reads)
	}
	req.Filters = map[string]*nanashiv1.Members{product: {Ids: []string{newMember}}}
	if reads, _ := queryReads(req, em, nil); len(reads) != 0 {
		t.Errorf("got %+v, want no read", reads)
	}
}

func TestQueryCells(t *testing.T) {
	cube := engineCube{Dims: []string{product}, Cells: [][]any{{memberA, 11.0}, {memberB, nil}}}
	rev, _ := model(t).metric(revenue)
	cells := queryCells(rev, []string{region, product}, cube, nil)
	if len(cells) != 1 || strings.Join(cells[0].Coords, ",") != ","+memberA || cells[0].Value.GetNumber() != 11 || cells[0].Metric != revenue {
		t.Errorf("got %v", cells)
	}
}

func TestQueryCellsHidesMemberValues(t *testing.T) {
	owner, _ := model(t).metric(own) // member:Region
	cube := engineCube{Dims: []string{product}, Cells: [][]any{{memberA, west}, {memberB, east}}}
	cells := queryCells(owner, []string{product}, cube, eastOnly())
	if len(cells) != 1 || cells[0].Coords[0] != memberB || cells[0].Value.GetMember() != east {
		t.Errorf("a reader limited to East must not see West as a value: %v", cells)
	}
}

func TestVisibleComments(t *testing.T) {
	em := model(t)
	l := eastOnly().through(em)
	comments := []*nanashiv1.Comment{
		{Id: "1", Cell: map[string]string{region: east, product: memberA}},
		{Id: "2", Cell: map[string]string{region: west}},
		{Id: "3"},
		{Id: "4", Cell: map[string]string{product: newMember}}, // A removed member: hidden for a reader with rules.
		{Id: "5", Cell: map[string]string{newMember: memberA}}, // A removed list.
	}
	var ids []string
	for _, c := range visibleComments(comments, l, em) {
		ids = append(ids, c.Id)
	}
	if got := strings.Join(ids, ","); got != "1,3" {
		t.Errorf("visible comments: %s, want 1,3", got)
	}
}
