package api

import (
	"cmp"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5/pgxpool"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

// TestAccessByRole needs PostgreSQL, but no engine: the interceptor refuses the calls before the engine.
func TestAccessByRole(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
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
	app := "api-test-" + newID()
	if _, err := pool.Exec(ctx, "insert into app_application (id, name) values ($1, 'test')", app); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "insert into app_member (app_id, user_name, role) values ($1, 'alice', 4), ($1, 'bob', 1)", app); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Exec(context.Background(), "delete from app_application where id = $1", app) })

	server := &PlanServer{Pool: pool, Engines: &Engines{}}
	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewPlanServiceHandler(server, connect.WithInterceptors(server.Interceptor())))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	client := nanashiv1connect.NewPlanServiceClient(srv.Client(), srv.URL)
	list := func(user string) (map[string]nanashiv1.Role, error) {
		req := connect.NewRequest(&nanashiv1.ListApplicationsRequest{})
		if user != "" {
			req.Header().Set("X-Nanashi-User", user)
		}
		res, err := client.ListApplications(ctx, req)
		if err != nil {
			return nil, err
		}
		out := map[string]nanashiv1.Role{}
		for _, a := range res.Msg.Applications {
			out[a.Id] = a.Role
		}
		return out, nil
	}
	if _, err := list(""); connect.CodeOf(err) != connect.CodeUnauthenticated {
		t.Errorf("no user: got %v, want unauthenticated", err)
	}
	for user, want := range map[string]nanashiv1.Role{"alice": admin, "bob": viewer, "carol": 0} {
		got, err := list(user)
		if err != nil || got[app] != want {
			t.Errorf("%s: got role %v (%v), want %v", user, got[app], err, want)
		}
	}
	write := connect.NewRequest(&nanashiv1.WriteCellsRequest{AppId: app})
	write.Header().Set("X-Nanashi-User", "bob")
	if _, err := client.WriteCells(ctx, write); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("viewer writes: got %v, want permission denied", err)
	}
	// The audit detail has the cell values of the requests, so the access rules of a VIEWER do not apply to it.
	audit := connect.NewRequest(&nanashiv1.ListAuditRequest{AppId: app})
	audit.Header().Set("X-Nanashi-User", "bob")
	if _, err := client.ListAudit(ctx, audit); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("viewer reads the audit trail: got %v, want permission denied", err)
	}
	read := connect.NewRequest(&nanashiv1.GetModelRequest{AppId: app})
	read.Header().Set("X-Nanashi-User", "carol")
	if _, err := client.GetModel(ctx, read); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("non-member reads: got %v, want permission denied", err)
	}
}
