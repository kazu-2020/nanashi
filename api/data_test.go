package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
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
		{Id: "1", Metric: budget, Cell: map[string]string{region: east, product: memberA}},
		{Id: "2", Metric: budget, Cell: map[string]string{region: west, product: memberA}},
		{Id: "3", Metric: own, Cell: map[string]string{product: memberA}},       // An input Metric without Region.
		{Id: "4", Metric: own, Cell: map[string]string{product: newMember}},     // A removed member: hidden for a reader with rules.
		{Id: "5", Metric: own, Cell: map[string]string{newMember: memberA}},     // A removed list.
		{Id: "6", Metric: revenue, Cell: map[string]string{product: memberA}},   // The limits hide Revenue.
		{Id: "7", Metric: budget, Cell: map[string]string{product: memberA}},    // A total over the limited Region.
		{Id: "8", Metric: newMember, Cell: map[string]string{product: memberA}}, // A removed Metric.
	}
	var ids []string
	for _, c := range visibleComments(comments, l, em) {
		ids = append(ids, c.Id)
	}
	if got := strings.Join(ids, ","); got != "1,3" {
		t.Errorf("visible comments: %s, want 1,3", got)
	}
}

// TestCommentsFollowAccessRules: a reader with rules sees only the comments on cells that a query shows, and
// AddComment takes only a cell with one member for each dimension of the Metric.
func TestCommentsFollowAccessRules(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	client := testClient(t, pool, newFakeEngine(t, sample))
	alice, bob := client("alice"), client("bob")
	if _, err := pool.Exec(ctx, "insert into app_access_rule (app_id, id, role, list, members, write) values ($1, $2, $3, $4, $5, false)",
		app, uuid.Must(uuid.NewV7()).String(), viewer, region, []string{east}); err != nil {
		t.Fatal(err)
	}
	add := func(metric string, cell map[string]string, body string) error {
		_, err := alice.AddComment(ctx, connect.NewRequest(&nanashiv1.AddCommentRequest{AppId: app, ClientOpId: uuid.NewString(),
			Comment: &nanashiv1.Comment{Id: uuid.Must(uuid.NewV7()).String(), Metric: metric, Cell: cell, Body: body}}))
		return err
	}
	for _, c := range []struct {
		metric string
		cell   map[string]string
	}{
		{budget, map[string]string{product: memberA}},                             // Region is missing.
		{budget, map[string]string{product: memberA, region: east, sales: sale1}}, // Sales is not a dimension of Budget.
		{budget, map[string]string{product: memberA, region: newMember}},          // The model does not have the member.
		{newMember, map[string]string{product: memberA}},                          // The model does not have the Metric.
	} {
		if err := add(c.metric, c.cell, "bad"); connect.CodeOf(err) != connect.CodeInvalidArgument {
			t.Errorf("AddComment %s %v: got %v, want InvalidArgument", c.metric, c.cell, err)
		}
	}
	for _, c := range []struct {
		metric string
		cell   map[string]string
		body   string
	}{
		{budget, map[string]string{product: memberA, region: east}, "east"},
		{budget, map[string]string{product: memberA, region: west}, "west"},
		{revenue, map[string]string{product: memberA}, "revenue"}, // Revenue can sum Region away, so the limits hide it.
		{own, map[string]string{product: memberA}, "owner"},       // An input Metric without Region.
	} {
		if err := add(c.metric, c.cell, c.body); err != nil {
			t.Fatal(err)
		}
	}
	// A comment on a total over Region, from a time before AddComment checked the cell.
	if _, err := pool.Exec(ctx, "insert into app_comment (app_id, id, metric, cell, user_name, body) values ($1, $2, $3, $4, 'alice', 'total')",
		app, uuid.Must(uuid.NewV7()).String(), budget, textJSON(map[string]string{product: memberA})); err != nil {
		t.Fatal(err)
	}
	bodies := func(c nanashiv1connect.PlanServiceClient) string {
		res, err := c.ListComments(ctx, connect.NewRequest(&nanashiv1.ListCommentsRequest{AppId: app}))
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, x := range res.Msg.Comments {
			out = append(out, x.Body)
		}
		slices.Sort(out)
		return strings.Join(out, ",")
	}
	if got := bodies(bob); got != "east,owner" {
		t.Errorf("bob sees %s, want east,owner", got)
	}
	if got := bodies(alice); got != "east,owner,revenue,total,west" {
		t.Errorf("alice sees %s, want all comments", got)
	}
}
