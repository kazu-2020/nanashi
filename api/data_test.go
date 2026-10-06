package api

import (
	"context"
	"encoding/json"
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
	}, em)
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
	if _, err := writeOps([]*nanashiv1.CellWrite{{Metric: newMember, Value: num(1)}}, em); err == nil {
		t.Errorf("unknown Metric: got %v, want an input error", err)
	}
}

func TestQueryReads(t *testing.T) {
	em := model(t)
	req := &nanashiv1.QueryRequest{Metrics: []string{budget, revenue, own, newMember}, Rows: []string{product}, Columns: []string{region},
		Filters: map[string]*nanashiv1.Members{product: {Ids: []string{memberA}}}}
	reads, err := queryReads(req, em)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := json.Marshal(reads)
	// Revenue has no Region, so it keeps Product only. Owner is a MEMBER Metric, so it gets a slice. An unknown Metric is skipped.
	want := fmt.Sprintf(`[{"Metric":%q,"Path":"summary","Query":{%q:[%q],"agg":["sum"],"keep":["%s,%s"]}},`+
		`{"Metric":%q,"Path":"summary","Query":{%q:[%q],"agg":["sum"],"keep":[%q]}},`+
		`{"Metric":%q,"Path":"slice","Query":{%q:[%q]}}]`, budget, product, memberA, product, region, revenue, product, memberA, product, own, product, memberA)
	if string(got) != want {
		t.Errorf("got %s\nwant %s", got, want)
	}
	// A filter that gives all members sends no filter: the ids of a big list do not fit in the URL.
	req.Filters = map[string]*nanashiv1.Members{region: {Ids: []string{east, west}}}
	if reads, _ := queryReads(req, em); reads[0].Query[region] != nil {
		t.Errorf("got %+v", reads[0].Query)
	}
	// A removed member in a filter is dropped, not sent to the engine. A filter of removed members only gives nothing.
	req.Filters = map[string]*nanashiv1.Members{product: {Ids: []string{memberA, newMember}}}
	if reads, _ := queryReads(req, em); reads[0].Query[product][0] != memberA {
		t.Errorf("got %+v", reads)
	}
	req.Filters = map[string]*nanashiv1.Members{product: {Ids: []string{newMember}}}
	if reads, _ := queryReads(req, em); len(reads) != 0 {
		t.Errorf("got %+v, want no read", reads)
	}
}

func TestQueryCells(t *testing.T) {
	cube := engineCube{Dims: []string{product}, Cells: [][]any{{memberA, 11.0}, {memberB, nil}}}
	rev, _ := model(t).metric(revenue)
	cells := queryCells(rev, []string{region, product}, cube)
	if len(cells) != 1 || strings.Join(cells[0].Coords, ",") != ","+memberA || cells[0].Value.GetNumber() != 11 || cells[0].Metric != revenue {
		t.Errorf("got %v", cells)
	}
}

// TestComments: AddComment takes only a cell with one member for each dimension of the Metric, and
// ListComments gives all comments.
func TestComments(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	client := testClient(t, pool, newFakeEngine(t, sample))
	alice := client("alice")
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
		{revenue, map[string]string{product: memberA}, "revenue"},
		{own, map[string]string{product: memberA}, "owner"}, // An input Metric without Region.
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
	if got := bodies(alice); got != "east,owner,revenue,total,west" {
		t.Errorf("alice sees %s, want all comments", got)
	}
}
