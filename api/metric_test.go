package api

import (
	"cmp"
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestMetricOpCreateAndUpdate(t *testing.T) {
	em := model(t)
	budgetDef := func(dims ...string) *nanashiv1.MetricDef {
		return &nanashiv1.MetricDef{Id: budget, Name: "Budget", Dimensions: dims}
	}
	if _, err := metricOp(em, budgetDef(product), "number", true); connect.CodeOf(connectError(err)) != connect.CodeAlreadyExists {
		t.Errorf("create of an existing Metric: got %v, want AlreadyExists", err)
	}
	if _, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "New"}, "number", false); connect.CodeOf(connectError(err)) != connect.CodeNotFound {
		t.Errorf("update of a missing Metric: got %v, want NotFound", err)
	}
	if _, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "Budget"}, "number", true); connect.CodeOf(connectError(err)) != connect.CodeAlreadyExists {
		t.Errorf("create with the name of another Metric: got %v, want AlreadyExists", err)
	}
	if ops, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "New"}, "number", true); err != nil ||
		opsJSON(t, ops) != fmt.Sprintf(`[{"cells":[],"dims":[],"id":%q,"kind":"number","name":"New","op":"add_input"}]`, newMember) {
		t.Errorf("create: got %s, %v", opsJSON(t, ops), err)
	}
	if ops, err := metricOp(em, budgetDef(product, region), "number", false); err != nil || len(ops) != 0 {
		t.Errorf("same input Metric: got %v, %v, want no operation", ops, err)
	}
	// Another user renamed the Metric. An update with the old name must not rename it back or delete its cells.
	stale := &nanashiv1.MetricDef{Id: budget, Name: "Old Budget", Dimensions: []string{product, region}}
	if ops, err := metricOp(em, stale, "number", false); err != nil || len(ops) != 0 {
		t.Errorf("input Metric with a stale name: got %s, %v, want no operation", opsJSON(t, ops), err)
	}
	stale.Formula = "1"
	if ops, err := metricOp(em, stale, "number", false); err != nil || !strings.Contains(opsJSON(t, ops), `"name":"Budget"`) {
		t.Errorf("formula with a stale name: got %s, %v, want the current name", opsJSON(t, ops), err)
	}
	for _, c := range []struct {
		m    *nanashiv1.MetricDef
		kind string
	}{
		{budgetDef(product), "number"},
		{budgetDef(product, region), "boolean"},
		{&nanashiv1.MetricDef{Id: budget, Name: "Budget", Dimensions: []string{product, region}, Formula: "1"}, "number"},
	} {
		if ops, err := metricOp(em, c.m, c.kind, false); err != nil || len(ops) != 1 {
			t.Errorf("update %v: got %v, %v", c.m, ops, err)
		}
	}
	if ops, err := metricOp(em, &nanashiv1.MetricDef{Id: revenue, Name: "Revenue", Dimensions: []string{product}, Formula: "2"}, "number", false); err != nil ||
		opsJSON(t, ops) != fmt.Sprintf(`[{"dims":[%q],"formula":"2","id":%q,"kind":"number","name":"Revenue","op":"add_formula","overridable":false}]`, product, revenue) {
		t.Errorf("formula change: got %s, %v", opsJSON(t, ops), err)
	}
	if _, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "New", Dimensions: []string{newMember}}, "number", true); err == nil {
		t.Error("an unknown list must be an error")
	}
}

func TestEngineKind(t *testing.T) {
	em := model(t)
	member, boolean := nanashiv1.ValueKind_VALUE_KIND_MEMBER, nanashiv1.ValueKind_VALUE_KIND_BOOLEAN
	for _, c := range []struct {
		m    *nanashiv1.MetricDef
		want string // Empty if the api refuses m.
	}{
		{&nanashiv1.MetricDef{}, "number"},
		{&nanashiv1.MetricDef{Kind: boolean}, "boolean"},
		{&nanashiv1.MetricDef{Kind: member, MemberList: region}, "member:" + region},
		{&nanashiv1.MetricDef{Kind: member, MemberList: newMember}, ""},
		{&nanashiv1.MetricDef{Kind: member}, ""},
		{&nanashiv1.MetricDef{Kind: boolean, MemberList: region}, ""},
	} {
		got, err := engineKind(em, c.m)
		if got != c.want || (err == nil) != (c.want != "") {
			t.Errorf("%v: got %q, %v, want %q", c.m, got, err, c.want)
		}
		if k, list := valueKind(got); err == nil && (k != cmp.Or(c.m.Kind, nanashiv1.ValueKind_VALUE_KIND_NUMBER) || list != c.m.MemberList) {
			t.Errorf("%v: valueKind(%q) gives %v, %q", c.m, got, k, list)
		}
	}
}

func TestCatalogStmt(t *testing.T) {
	m := &nanashiv1.MetricDef{Id: budget, Name: "Budget", Description: " Plan ", Folder: "P&L", Owner: "mallory"}
	stmts, made := catalogStmt("app", "alice", m, metricRow{}, false, true)
	if len(stmts) != 1 || stmts[0].args[4] != "alice" || stmts[0].args[2] != "Plan" || len(made) != 1 || made[0].Old != nil {
		t.Errorf("create: got %v, %v, want one insert with the caller as the owner", stmts, made)
	}
	if stmts, _ := catalogStmt("app", "alice", m, metricRow{Description: "Plan", Folder: "P&L", Owner: "bob"}, true, false); len(stmts) != 0 {
		t.Errorf("update without a catalog change: got %v, want no statement", stmts)
	}
	_, made = catalogStmt("app", "alice", m, metricRow{Description: "Old", Folder: "F", Owner: "bob"}, true, false)
	if got := compensation("app", made); len(got) != 1 || fmt.Sprint(got[0].args) != "[app "+budget+" Plan P&L Old F]" {
		t.Errorf("update compensation: got %v, want the old values back where the row has the new values", got)
	}
}

// TestMetricCatalog: CreateMetric writes the catalog row in the first transaction of the outbox, GetModel merges
// it, and a refusal of the engine deletes it (create) or puts the old values back (update).
func TestMetricCatalog(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	rowsAtWrite := -1
	refuse := false
	e.reply = func(int, map[string]any) (int, string) {
		rowsAtWrite = count(t, ctx, pool, "select count(*) from app_metric where app_id = $1", app)
		if refuse {
			return 400, `{"error": "bad_request", "message": "式が誤り"}`
		}
		return 200, `{"seq": 8}`
	}
	alice := testClient(t, pool, e)("alice")
	id := uuid.Must(uuid.NewV7()).String()
	def := &nanashiv1.MetricDef{Id: id, Name: "New", Description: "d", Folder: "f", Owner: "mallory"}
	if _, err := alice.CreateMetric(ctx, connect.NewRequest(&nanashiv1.CreateMetricRequest{AppId: app, ClientOpId: uuid.NewString(), Metric: def})); err != nil {
		t.Fatal(err)
	}
	if rowsAtWrite != 1 {
		t.Errorf("catalog rows when the engine got the write: %d, want 1 (the first transaction makes the row)", rowsAtWrite)
	}
	if n := count(t, ctx, pool, "select count(*) from app_metric where app_id = $1 and metric_id = $2 and owner = 'alice' and description = 'd' and folder = 'f'", app, id); n != 1 {
		t.Errorf("catalog row with alice as the owner: %d, want 1", n)
	}

	// GetModel: the engine model is the sample, so the new Metric and a catalog row of an unknown Metric do not show.
	if _, err := pool.Exec(ctx, "insert into app_metric (app_id, metric_id, description, owner) values ($1, $2, 'b', 'bob')", app, budget); err != nil {
		t.Fatal(err)
	}
	res, err := alice.GetModel(ctx, connect.NewRequest(&nanashiv1.GetModelRequest{AppId: app}))
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, m := range res.Msg.Metrics {
		got[m.Id] = m.Description + "/" + m.Folder + "/" + m.Owner
	}
	if len(got) != 4 || got[budget] != "b//bob" || got[revenue] != "//" || got[id] != "" {
		t.Errorf("GetModel catalog values: %v", got)
	}

	refuse = true
	refused := uuid.Must(uuid.NewV7()).String()
	_, err = alice.CreateMetric(ctx, connect.NewRequest(&nanashiv1.CreateMetricRequest{AppId: app, ClientOpId: uuid.NewString(),
		Metric: &nanashiv1.MetricDef{Id: refused, Name: "Bad", Formula: "x +", Description: "d"}}))
	if connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("refused create: got %v, want InvalidArgument", err)
	}
	if n := count(t, ctx, pool, "select count(*) from app_metric where app_id = $1 and metric_id = $2", app, refused); n != 0 {
		t.Errorf("a refused create must leave no catalog row, %d left", n)
	}
	_, err = alice.UpdateMetric(ctx, connect.NewRequest(&nanashiv1.UpdateMetricRequest{AppId: app, ClientOpId: uuid.NewString(),
		Metric: &nanashiv1.MetricDef{Id: revenue, Name: "Revenue", Dimensions: []string{product}, Formula: "x +", Description: "new"}}))
	if connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("refused update: got %v, want InvalidArgument", err)
	}
	if n := count(t, ctx, pool, "select count(*) from app_metric where app_id = $1 and metric_id = $2", app, revenue); n != 0 {
		t.Errorf("a refused update of a Metric without a row must leave no row, %d left", n)
	}
	_, err = alice.UpdateMetric(ctx, connect.NewRequest(&nanashiv1.UpdateMetricRequest{AppId: app, ClientOpId: uuid.NewString(),
		Metric: &nanashiv1.MetricDef{Id: budget, Name: "Budget", Dimensions: []string{product}, Description: "new"}}))
	if connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("refused update: got %v, want InvalidArgument", err)
	}
	if n := count(t, ctx, pool, "select count(*) from app_metric where app_id = $1 and metric_id = $2 and description = 'b' and owner = 'bob'", app, budget); n != 1 {
		t.Errorf("a refused update must put the old values back")
	}
}
