package api

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"google.golang.org/protobuf/proto"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

// testPool gives a pool on the test database with the api tables. If PostgreSQL is missing, it skips the test.
func testPool(t *testing.T, ctx context.Context) *pgxpool.Pool {
	t.Helper()
	// If NANASHI_PG_DSN is set (as in CI), a missing database is a failure, not a skip.
	skip := t.Skipf
	if os.Getenv("NANASHI_PG_DSN") != "" {
		skip = t.Fatalf
	}
	dsn := cmp.Or(os.Getenv("NANASHI_PG_DSN"), "postgresql://postgres@127.0.0.1:55432/nanashi")
	pool, err := pgxpool.New(ctx, dsn)
	if err == nil {
		err = pool.Ping(ctx)
	}
	if err != nil {
		skip("PostgreSQL (%s) is not reachable: %v", dsn, err)
	}
	t.Cleanup(pool.Close)
	if err := Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	return pool
}

// The ids of the sample model.
const (
	product, sales, region       = "0192f3a4-0000-7000-8000-000000000001", "0192f3a4-0000-7000-8000-000000000002", "0192f3a4-0000-7000-8000-000000000003"
	memberA, memberB             = "0192f3a4-0000-7000-8000-000000000011", "0192f3a4-0000-7000-8000-000000000012"
	sale1, sale2, sale9          = "0192f3a4-0000-7000-8000-000000000021", "0192f3a4-0000-7000-8000-000000000022", "0192f3a4-0000-7000-8000-000000000029"
	east, west                   = "0192f3a4-0000-7000-8000-000000000031", "0192f3a4-0000-7000-8000-000000000032"
	salesProduct                 = "0192f3a4-0000-7000-8000-000000000041"
	amount, budget, revenue, own = "0192f3a4-0000-7000-8000-000000000051", "0192f3a4-0000-7000-8000-000000000052", "0192f3a4-0000-7000-8000-000000000053", "0192f3a4-0000-7000-8000-000000000054"
)

// sample is an engine model (GET /) with a transaction list Sales that maps to Product.
var sample = `{"seq": 7,
 "dimensions": {
  "` + product + `": {"name": "Product", "members": [{"id": "` + memberB + `", "name": "B"}, {"id": "` + memberA + `", "name": "A"}], "ordered": false, "properties": {}, "property_values": {}},
  "` + sales + `": {"name": "Sales", "members": [{"id": "` + sale1 + `", "name": "1"}, {"id": "` + sale2 + `", "name": "2"}, {"id": "` + sale9 + `", "name": "9"}], "ordered": false,
            "properties": {"` + salesProduct + `": {"name": "Product", "target": "` + product + `"}},
            "property_values": {"` + salesProduct + `": {"` + sale1 + `": "` + memberA + `", "` + sale2 + `": "` + memberB + `"}}},
  "` + region + `": {"name": "Region", "members": [{"id": "` + east + `", "name": "East"}, {"id": "` + west + `", "name": "West"}], "ordered": false, "properties": {}, "property_values": {}}},
 "metrics": {
  "` + amount + `": {"name": "Sales.Amount", "dims": ["` + sales + `"], "kind": "number", "overridable": false, "formula": null},
  "` + budget + `": {"name": "Budget", "dims": ["` + product + `", "` + region + `"], "kind": "number", "overridable": false, "formula": null},
  "` + revenue + `": {"name": "Revenue", "dims": ["` + product + `"], "kind": "number", "overridable": false, "formula": "'Sales.Amount'[BY SUM: Sales.Product]"},
  "` + own + `": {"name": "Owner", "dims": ["` + product + `"], "kind": "member:` + region + `", "overridable": false, "formula": null}}}`

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
	return accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: region, Members: []string{east}, Write: true}}, sampleModel())
}

func sampleModel() engineModel {
	em, _ := parseEngineModel([]byte(sample))
	return em
}

func TestConnectError(t *testing.T) {
	for _, c := range []struct {
		err  error
		code connect.Code
	}{
		{errors.New("入力の誤り"), connect.CodeInvalidArgument},
		{tag(errDenied, "権限がない"), connect.CodePermissionDenied},
		{fmt.Errorf("3 行目: %w", tag(errDenied, "権限がない")), connect.CodePermissionDenied},
		{tag(errExists, "ある"), connect.CodeAlreadyExists},
		{tag(errPrecondition, "消える"), connect.CodeFailedPrecondition},
		{connect.NewError(connect.CodeUnavailable, errors.New("x")), connect.CodeUnavailable},
	} {
		got := connectError(c.err)
		if connect.CodeOf(got) != c.code || !strings.Contains(got.Error(), c.err.Error()) {
			t.Errorf("%v: got %v, want code %v with the same message", c.err, got, c.code)
		}
	}
}

// TestRPCRulesCoverAllMethods makes sure that the interceptor does not refuse an RPC of PlanService.
func TestRPCRulesCoverAllMethods(t *testing.T) {
	methods := nanashiv1.File_nanashi_v1_plan_proto.Services().ByName("PlanService").Methods()
	for i := range methods.Len() {
		name := string(methods.Get(i).Name())
		if _, ok := rpcRules[name]; !ok {
			t.Errorf("rpcRules has no entry for %s", name)
		}
	}
}

func TestDBErrorGivesAlreadyExistsByConstraint(t *testing.T) {
	err := dbError(fmt.Errorf("insert: %w", &pgconn.PgError{Code: "23505", ConstraintName: "app_property_name"}))
	if connect.CodeOf(err) != connect.CodeAlreadyExists || !strings.Contains(err.Error(), "名前") {
		t.Fatalf("got %v, want AlreadyExists about the name", err)
	}
	if connect.CodeOf(dbError(errors.New("connection reset"))) != connect.CodeUnavailable {
		t.Fatal("another database error must stay Unavailable")
	}
	if connect.CodeOf(dbError(tag(errNotFound, "x"))) != connect.CodeNotFound {
		t.Fatal("a tagged error must keep its code")
	}
}

func TestCheckID(t *testing.T) {
	if err := checkID("id", "0192f3a4-5b6c-7d8e-9f01-23456789abcd"); err != nil {
		t.Errorf("canonical form: got %v", err)
	}
	for _, s := range []string{"", "app-1a2b3c4d5e6f7a8b", "0192f3a4-5b6c-7d8e-9f01", "0192f3a4-5b6c-7d8e-9f01-23456789abcz",
		"0192f3a45b6c7d8e9f0123456789abcd", "0192F3A4-5B6C-7D8E-9F01-23456789ABCD", "{0192f3a4-5b6c-7d8e-9f01-23456789abcd}",
		"urn:uuid:0192f3a4-5b6c-7d8e-9f01-23456789abcd"} {
		if err := checkID("id", s); connect.CodeOf(err) != connect.CodeInvalidArgument {
			t.Errorf("%q: got %v, want InvalidArgument", s, err)
		}
	}
}

func TestCheckRequest(t *testing.T) {
	up := strings.ToUpper
	ok := []proto.Message{
		&nanashiv1.QueryRequest{AppId: product, Metrics: []string{budget}, Rows: []string{product},
			Filters: map[string]*nanashiv1.Members{region: {Ids: []string{east}}}},
		// A name is not an id, and an optional reference stays empty.
		&nanashiv1.EditMembersRequest{AppId: product, List: sales, Edits: []*nanashiv1.MemberEdit{
			{Edit: &nanashiv1.MemberEdit_Add{Add: &nanashiv1.AddMember{Id: sale1, Name: "Not An Id", Properties: map[string]string{salesProduct: "text"}}}}}},
		&nanashiv1.WriteCellsRequest{Writes: []*nanashiv1.CellWrite{{Metric: budget, Coords: map[string]string{product: memberA}}}},
	}
	for _, m := range ok {
		before := proto.Clone(m)
		if err := checkRequest(m); err != nil || !proto.Equal(m, before) {
			t.Errorf("%T: got %v, want no error and no change", m, err)
		}
	}
	// Each place that holds an id refuses an id that is not in the canonical form.
	bad := []proto.Message{
		&nanashiv1.CreateMetricRequest{Metric: &nanashiv1.MetricDef{Id: "nope"}},
		&nanashiv1.QueryRequest{AppId: up(product)},
		&nanashiv1.QueryRequest{AppId: product, Metrics: []string{up(budget)}},
		&nanashiv1.QueryRequest{AppId: product, Filters: map[string]*nanashiv1.Members{up(region): {Ids: []string{east}}}},
		&nanashiv1.QueryRequest{AppId: product, Filters: map[string]*nanashiv1.Members{region: {Ids: []string{up(east)}}}},
		&nanashiv1.WriteCellsRequest{Writes: []*nanashiv1.CellWrite{{Metric: budget, Coords: map[string]string{product: up(memberA)}}}},
		&nanashiv1.EditMembersRequest{AppId: product, List: sales, Edits: []*nanashiv1.MemberEdit{
			{Edit: &nanashiv1.MemberEdit_Add{Add: &nanashiv1.AddMember{Id: sale1, Name: "x", Properties: map[string]string{up(salesProduct): "text"}}}}}},
	}
	for i, m := range bad {
		if err := checkRequest(m); connect.CodeOf(err) != connect.CodeInvalidArgument {
			t.Errorf("bad request %d (%T): got %v, want InvalidArgument", i, m, err)
		}
	}
}

func TestRequestHashIgnoresClientOpID(t *testing.T) {
	a := &nanashiv1.CreateListRequest{AppId: product, Id: sales, Name: "x", ClientOpId: uuid.NewString()}
	b := &nanashiv1.CreateListRequest{AppId: product, Id: sales, Name: "x", ClientOpId: uuid.NewString()}
	c := &nanashiv1.CreateListRequest{AppId: product, Id: sales, Name: "y", ClientOpId: a.ClientOpId}
	if requestHash(a) != requestHash(b) || requestHash(a) == requestHash(c) {
		t.Error("the hash must depend on the content and not on the client_op_id")
	}
}

// fakeEngine is a router with one model for the tests. Reply decides the answer to each write (by its count).
type fakeEngine struct {
	mu     sync.Mutex
	model  string
	writes []map[string]any
	gets   int
	reply  func(n int, body map[string]any) (int, string)
	server *httptest.Server
}

func newFakeEngine(t *testing.T, model string) *fakeEngine {
	t.Helper()
	e := &fakeEngine{model: model, reply: func(int, map[string]any) (int, string) { return 200, `{"seq": 8}` }}
	e.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		e.mu.Lock()
		defer e.mu.Unlock()
		switch {
		case r.Method == "PUT":
			io.WriteString(w, `{"ok": true}`)
		case r.Method == "POST":
			var body map[string]any
			b, _ := io.ReadAll(r.Body)
			json.Unmarshal(b, &body)
			e.writes = append(e.writes, body)
			status, reply := e.reply(len(e.writes), body)
			e.mu.Unlock()
			if status == 0 { // No answer: the client times out.
				time.Sleep(300 * time.Millisecond)
			}
			e.mu.Lock()
			w.WriteHeader(cmp.Or(status, 200))
			io.WriteString(w, reply)
		case strings.HasSuffix(r.URL.Path, "/"):
			e.gets++
			io.WriteString(w, e.model)
		default:
			io.WriteString(w, `{"seq": 7, "dims": [], "cells": []}`)
		}
	}))
	t.Cleanup(e.server.Close)
	return e
}

// opIDs gives the client_op_id of each write that the engine got.
func (e *fakeEngine) opIDs() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	var out []string
	for _, w := range e.writes {
		out = append(out, w["client_op_id"].(string))
	}
	return out
}

// testApp makes an application with alice as ADMIN and bob as VIEWER, whose creation is done.
func testApp(t *testing.T, ctx context.Context, pool *pgxpool.Pool) string {
	t.Helper()
	app := uuid.Must(uuid.NewV7()).String()
	for _, sql := range []string{
		"insert into app_application (id, name) values ($1, 'test')",
		"insert into app_member (app_id, user_name, role) values ($1, 'alice', 4), ($1, 'bob', 1)",
		"insert into app_operation (app_id, client_op_id, user_name, method, request_hash, status) values ($1, $1, 'alice', 'CreateApplication', '', 'done')",
	} {
		if _, err := pool.Exec(ctx, sql, app); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		pool.Exec(context.Background(), "delete from app_application where id = $1", app)
		pool.Exec(context.Background(), "delete from app_operation where app_id = $1", app)
	})
	return app
}

// testClient gives a client of a PlanServer on the pool and the fake engine. The HTTP client of the engine
// times out after 100 ms.
func testClient(t *testing.T, pool *pgxpool.Pool, e *fakeEngine) func(user string) nanashiv1connect.PlanServiceClient {
	t.Helper()
	engines := &Engines{HTTP: &http.Client{Timeout: 100 * time.Millisecond}}
	if e != nil {
		engines.Router = e.server.URL
	}
	server := &PlanServer{Pool: pool, Engines: engines}
	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewPlanServiceHandler(server, connect.WithInterceptors(server.Interceptor())))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return func(user string) nanashiv1connect.PlanServiceClient {
		header := connect.WithInterceptors(connect.UnaryInterceptorFunc(func(next connect.UnaryFunc) connect.UnaryFunc {
			return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
				if user != "" {
					req.Header().Set("X-Nanashi-User", user)
				}
				return next(ctx, req)
			}
		}))
		return nanashiv1connect.NewPlanServiceClient(srv.Client(), srv.URL, header)
	}
}

func opStatus(t *testing.T, ctx context.Context, pool *pgxpool.Pool, app, opID string) string {
	t.Helper()
	var status string
	if err := pool.QueryRow(ctx, "select status from app_operation where app_id = $1 and client_op_id = $2", app, opID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	return status
}

func count(t *testing.T, ctx context.Context, pool *pgxpool.Pool, sql string, args ...any) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(ctx, sql, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// TestOutboxSettlesAPendingRowOnce: a write without an answer leaves the row pending. The next change settles it,
// and the engine gets its client_op_id again (it does not commit two times). A resend then returns the stored result.
func TestOutboxSettlesAPendingRowOnce(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	e.reply = func(n int, _ map[string]any) (int, string) {
		if n == 1 {
			return 0, "" // No answer.
		}
		return 200, `{"seq": 9}`
	}
	alice := testClient(t, pool, e)("alice")
	op1, list1 := uuid.NewString(), uuid.Must(uuid.NewV7()).String()
	create := func(opID, id, name string) error {
		_, err := alice.CreateList(ctx, connect.NewRequest(&nanashiv1.CreateListRequest{AppId: app, ClientOpId: opID, Id: id, Name: name, Kind: nanashiv1.ListKind_LIST_KIND_DIMENSION}))
		return err
	}
	if err := create(op1, list1, "Pending"); connect.CodeOf(err) != connect.CodeUnavailable {
		t.Fatalf("first create: got %v, want Unavailable", err)
	}
	if got := opStatus(t, ctx, pool, app, op1); got != "pending" {
		t.Fatalf("after the lost answer: status %s, want pending", got)
	}
	op2 := uuid.NewString()
	if err := create(op2, uuid.Must(uuid.NewV7()).String(), "Next"); err != nil {
		t.Fatalf("second create: %v", err)
	}
	if got := e.opIDs(); len(got) != 3 || got[0] != op1 || got[1] != op1 || got[2] != op2 {
		t.Fatalf("engine writes: got %v, want op1, op1 again, op2", got)
	}
	if got := opStatus(t, ctx, pool, app, op1); got != "done" {
		t.Fatalf("after the settle: status %s, want done", got)
	}
	if err := create(op1, list1, "Pending"); err != nil {
		t.Fatalf("resend of a done operation: %v", err)
	}
	if got := e.opIDs(); len(got) != 3 {
		t.Errorf("a resend of a done operation must not reach the engine: %v", got)
	}
	if n := count(t, ctx, pool, "select count(*) from app_list where app_id = $1", app); n != 2 {
		t.Errorf("app_list rows: %d, want 2", n)
	}
}

// TestOutboxRefusalIsFinal: a 400 of the engine makes the row failed, deletes the rows of the first transaction,
// and a resend with the same client_op_id returns the stored error without the engine.
func TestOutboxRefusalIsFinal(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	e.reply = func(int, map[string]any) (int, string) {
		return 400, `{"error": "bad_request", "message": "式が誤り"}`
	}
	alice := testClient(t, pool, e)("alice")
	opID := uuid.NewString()
	req := func() *connect.Request[nanashiv1.AddPropertyRequest] {
		return connect.NewRequest(&nanashiv1.AddPropertyRequest{AppId: app, ClientOpId: opID, List: product,
			Property: &nanashiv1.PropertyDef{Id: uuid.Must(uuid.NewV7()).String(), Name: "Cost", Type: nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER}})
	}
	first := req()
	_, err := alice.AddProperty(ctx, first)
	if connect.CodeOf(err) != connect.CodeInvalidArgument || !strings.Contains(err.Error(), "式が誤り") {
		t.Fatalf("got %v, want the engine message as InvalidArgument", err)
	}
	if got := opStatus(t, ctx, pool, app, opID); got != "failed" {
		t.Fatalf("status %s, want failed", got)
	}
	if n := count(t, ctx, pool, "select count(*) from app_property where app_id = $1", app); n != 0 {
		t.Errorf("the compensation must delete the property row, %d left", n)
	}
	_, again := alice.AddProperty(ctx, first)
	if connect.CodeOf(again) != connect.CodeInvalidArgument || !strings.Contains(again.Error(), "式が誤り") || len(e.opIDs()) != 1 {
		t.Errorf("resend: got %v after %d engine writes, want the stored error and no new write", again, len(e.opIDs()))
	}
	// The same client_op_id with another content is an error.
	_, other := alice.AddProperty(ctx, req())
	if connect.CodeOf(other) != connect.CodeInvalidArgument || !strings.Contains(other.Error(), "client_op_id") {
		t.Errorf("another request with the same client_op_id: got %v, want InvalidArgument", other)
	}
}

// TestOutboxConstraintStopsBeforeTheEngine: a create with an id that a row has gives AlreadyExists, and the
// engine gets nothing.
func TestOutboxConstraintStopsBeforeTheEngine(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	alice := testClient(t, pool, e)("alice")
	id := uuid.Must(uuid.NewV7()).String()
	if _, err := pool.Exec(ctx, "insert into app_list (app_id, id, kind) values ($1, $2, 1)", app, id); err != nil {
		t.Fatal(err)
	}
	_, err := alice.CreateList(ctx, connect.NewRequest(&nanashiv1.CreateListRequest{AppId: app, ClientOpId: uuid.NewString(), Id: id, Name: "Dup", Kind: nanashiv1.ListKind_LIST_KIND_DIMENSION}))
	if connect.CodeOf(err) != connect.CodeAlreadyExists {
		t.Fatalf("got %v, want AlreadyExists", err)
	}
	if n := len(e.opIDs()); n != 0 {
		t.Errorf("the engine got %d writes, want 0", n)
	}
	if n := count(t, ctx, pool, "select count(*) from app_operation where app_id = $1 and status = 'pending'", app); n != 0 {
		t.Errorf("%d pending rows, want 0: the first transaction must roll back", n)
	}
}

// TestRenameMetricChangesNoAPIRow: the tables and the comments refer to the Metric by id.
func TestRenameMetricChangesNoAPIRow(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	alice := testClient(t, pool, e)("alice")
	table, comment := uuid.Must(uuid.NewV7()).String(), uuid.Must(uuid.NewV7()).String()
	if _, err := alice.CreateTable(ctx, connect.NewRequest(&nanashiv1.CreateTableRequest{AppId: app, ClientOpId: uuid.NewString(),
		Table: &nanashiv1.TableDef{Id: table, Name: "T", Metrics: []string{budget}}})); err != nil {
		t.Fatal(err)
	}
	if _, err := alice.AddComment(ctx, connect.NewRequest(&nanashiv1.AddCommentRequest{AppId: app, ClientOpId: uuid.NewString(),
		Comment: &nanashiv1.Comment{Id: comment, Metric: budget, Cell: map[string]string{product: memberA, region: east}, Body: "hi"}})); err != nil {
		t.Fatal(err)
	}
	before := count(t, ctx, pool, "select count(*) from app_item i, app_comment c where i.app_id = $1 and c.app_id = $1 and i.def->'metrics' ? $2 and c.metric = $2::uuid", app, budget)
	if _, err := alice.RenameMetric(ctx, connect.NewRequest(&nanashiv1.RenameMetricRequest{AppId: app, ClientOpId: uuid.NewString(), Id: budget, Name: "Budget 2027"})); err != nil {
		t.Fatal(err)
	}
	after := count(t, ctx, pool, "select count(*) from app_item i, app_comment c where i.app_id = $1 and c.app_id = $1 and i.def->'metrics' ? $2 and c.metric = $2::uuid", app, budget)
	if before != 1 || after != 1 {
		t.Errorf("rows that refer to the Metric: %d before, %d after, want 1 and 1", before, after)
	}
	if w := e.writes; len(w) != 1 || w[0]["ops"].([]any)[0].(map[string]any)["op"] != "rename_metric" {
		t.Errorf("engine writes: %v", w)
	}
}

// TestCreateSnapshotRetriesWhenTheVersionChanges: a change of the api rows between the engine read and the row
// read makes CreateSnapshot read again.
func TestCreateSnapshotRetriesWhenTheVersionChanges(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	e.server.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/") {
			e.mu.Lock()
			e.gets++
			first := e.gets == 1
			e.mu.Unlock()
			if first { // A change of another process between the two reads.
				pool.Exec(ctx, "update app_application set version = version + 1 where id = $1", app)
			}
			io.WriteString(w, sample)
			return
		}
		io.WriteString(w, `{"seq": 7, "dims": [], "cells": []}`)
	})
	alice := testClient(t, pool, e)("alice")
	id := uuid.Must(uuid.NewV7()).String()
	res, err := alice.CreateSnapshot(ctx, connect.NewRequest(&nanashiv1.CreateSnapshotRequest{AppId: app, ClientOpId: uuid.NewString(), Id: id, Name: "s"}))
	if err != nil || res.Msg.Id != id {
		t.Fatalf("got %v, %v", res, err)
	}
	if e.gets != 2 {
		t.Errorf("engine model reads: %d, want 2 (one read again after the version changed)", e.gets)
	}
	if n := count(t, ctx, pool, "select count(*) from app_snapshot where id = $1", id); n != 1 {
		t.Errorf("snapshots: %d, want 1", n)
	}
}

// TestOutboxTooLargeIsFinal: a 413 of the router or the engine is a refusal: the row is failed, the client gets
// ResourceExhausted, and the next change of the application is not blocked.
func TestOutboxTooLargeIsFinal(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	e.reply = func(n int, _ map[string]any) (int, string) {
		if n == 1 {
			return 413, `{"error": "too_large", "message": "本文は 16 MiB まで"}`
		}
		return 200, `{"seq": 8}`
	}
	alice := testClient(t, pool, e)("alice")
	opID := uuid.NewString()
	create := func(opID, name string) error {
		_, err := alice.CreateList(ctx, connect.NewRequest(&nanashiv1.CreateListRequest{AppId: app, ClientOpId: opID, Id: uuid.Must(uuid.NewV7()).String(), Name: name, Kind: nanashiv1.ListKind_LIST_KIND_DIMENSION}))
		return err
	}
	if err := create(opID, "Big"); connect.CodeOf(err) != connect.CodeResourceExhausted {
		t.Fatalf("got %v, want ResourceExhausted", err)
	}
	if got := opStatus(t, ctx, pool, app, opID); got != "failed" {
		t.Fatalf("status %s, want failed", got)
	}
	if err := create(uuid.NewString(), "Next"); err != nil {
		t.Fatalf("the next change: %v", err)
	}
}

// TestOutboxUnknownRowDoesNotBlock: a row whose engine result stays unknown does not block the next change. The
// engine orders the writes, so a later settle of the row is a later write.
func TestOutboxUnknownRowDoesNotBlock(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	e.reply = func(n int, body map[string]any) (int, string) {
		if n <= 2 { // The first send and the settle by the next change get no answer.
			return 0, ""
		}
		return 200, `{"seq": 9}`
	}
	alice := testClient(t, pool, e)("alice")
	op1, op2 := uuid.NewString(), uuid.NewString()
	list1 := uuid.Must(uuid.NewV7()).String()
	create := func(opID, id, name string) error {
		_, err := alice.CreateList(ctx, connect.NewRequest(&nanashiv1.CreateListRequest{AppId: app, ClientOpId: opID, Id: id, Name: name, Kind: nanashiv1.ListKind_LIST_KIND_DIMENSION}))
		return err
	}
	if err := create(op1, list1, "Stuck"); connect.CodeOf(err) != connect.CodeUnavailable {
		t.Fatalf("first create: got %v, want Unavailable", err)
	}
	if err := create(op2, uuid.Must(uuid.NewV7()).String(), "Next"); err != nil {
		t.Fatalf("the next change must not wait for the unknown row: %v", err)
	}
	if got := e.opIDs(); len(got) != 3 || got[1] != op1 || got[2] != op2 {
		t.Fatalf("engine writes: got %v, want op1, op1 again, op2", got)
	}
	if got := opStatus(t, ctx, pool, app, op1); got != "pending" {
		t.Fatalf("after the next change: status %s, want pending", got)
	}
	if err := create(op1, list1, "Stuck"); err != nil {
		t.Fatalf("resend after the engine answers: %v", err)
	}
	if got := opStatus(t, ctx, pool, app, op1); got != "done" {
		t.Fatalf("after the resend: status %s, want done", got)
	}
}

func auditCount(t *testing.T, ctx context.Context, pool *pgxpool.Pool, app, opID string) int {
	t.Helper()
	return count(t, ctx, pool, "select count(*) from app_audit where app_id = $1 and detail like '%' || $2 || '%'", app, opID)
}

// TestAuditOnceForEachOperation: an operation gets one audit row when it is done, also when another request
// settles it. A resend adds no row. An api-only change audits in its own transaction, one time.
func TestAuditOnceForEachOperation(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	e := newFakeEngine(t, sample)
	e.reply = func(n int, _ map[string]any) (int, string) {
		if n == 1 {
			return 0, ""
		}
		return 200, `{"seq": 9}`
	}
	alice := testClient(t, pool, e)("alice")
	op1, op2, op3 := uuid.NewString(), uuid.NewString(), uuid.NewString()
	list1 := uuid.Must(uuid.NewV7()).String()
	create := func(opID, id, name string) error {
		_, err := alice.CreateList(ctx, connect.NewRequest(&nanashiv1.CreateListRequest{AppId: app, ClientOpId: opID, Id: id, Name: name, Kind: nanashiv1.ListKind_LIST_KIND_DIMENSION}))
		return err
	}
	if err := create(op1, list1, "Pending"); connect.CodeOf(err) != connect.CodeUnavailable {
		t.Fatalf("first create: got %v, want Unavailable", err)
	}
	if err := create(op2, uuid.Must(uuid.NewV7()).String(), "Next"); err != nil {
		t.Fatal(err)
	}
	if n := auditCount(t, ctx, pool, app, op1); n != 1 {
		t.Errorf("audit rows of the operation that the next change settled: %d, want 1", n)
	}
	if err := create(op1, list1, "Pending"); err != nil {
		t.Fatal(err)
	}
	if n := auditCount(t, ctx, pool, app, op1); n != 1 {
		t.Errorf("audit rows after a resend: %d, want 1", n)
	}
	if n := auditCount(t, ctx, pool, app, op2); n != 1 {
		t.Errorf("audit rows of the next change: %d, want 1", n)
	}
	table := &nanashiv1.TableDef{Id: uuid.Must(uuid.NewV7()).String(), Name: "T", Metrics: []string{budget}}
	for range 2 {
		if _, err := alice.CreateTable(ctx, connect.NewRequest(&nanashiv1.CreateTableRequest{AppId: app, ClientOpId: op3, Table: table})); err != nil {
			t.Fatal(err)
		}
	}
	if n := auditCount(t, ctx, pool, app, op3); n != 1 {
		t.Errorf("audit rows of an api-only change sent two times: %d, want 1", n)
	}
}
