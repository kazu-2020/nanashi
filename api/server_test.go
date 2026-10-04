package api

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5/pgxpool"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// testPool gives a pool on the test database with the api tables. Without PostgreSQL, it skips the test.
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

func TestAppRename(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	for _, c := range []struct {
		old  string
		name *string
		want string
	}{
		{"b", ptr("B"), `["c", "B", "a"]`},
		{"b", nil, `["c", "a"]`},
		{"x", ptr("y"), `["c", "b", "a"]`},
	} {
		var got string
		if err := pool.QueryRow(ctx, `select app_rename('["c", "b", "a"]', $1, $2)::text`, c.old, c.name).Scan(&got); err != nil {
			t.Fatal(err)
		}
		if got != c.want {
			t.Errorf("app_rename(%q, %v): got %s, want %s", c.old, c.name, got, c.want)
		}
	}
	var got string
	if err := pool.QueryRow(ctx, `select app_rename('["a"]', 'a', null)::text`).Scan(&got); err != nil || got != "[]" {
		t.Errorf("removing the last name: got %s, %v, want []", got, err)
	}
}

func ptr(s string) *string { return &s }

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

// sample is an engine model (GET /) with a transaction list Sales that maps to Product.
const sample = `{"seq": 7,
 "dimensions": {
  "Product": {"members": ["B", "A"], "ordered": false, "properties": {}, "property_values": {}},
  "Sales": {"members": ["1", "2", "9"], "ordered": false, "properties": {"Product": "Product"},
            "property_values": {"Product": {"1": "A", "2": "B"}}},
  "Region": {"members": ["East", "West"], "ordered": false, "properties": {}, "property_values": {}}},
 "metrics": {
  "Sales.Amount": {"dims": ["Sales"], "kind": "number", "overridable": false, "formula": null},
  "Budget": {"dims": ["Product", "Region"], "kind": "number", "overridable": false, "formula": null},
  "Revenue": {"dims": ["Product"], "kind": "number", "overridable": false, "formula": "'Sales.Amount'[BY SUM: Sales.Product]"},
  "Owner": {"dims": ["Product"], "kind": "member:Region", "overridable": false, "formula": null}}}`

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
	return accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: "Region", Members: []string{"East"}, Write: true}})
}
