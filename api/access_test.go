package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

func TestAccessLimits(t *testing.T) {
	rules := []*nanashiv1.AccessRule{
		{Role: viewer, List: "Region", Members: []string{"East", "West"}, Write: true},
		{Role: viewer, List: "Region", Members: []string{"East"}},
		{Role: contributor, List: "Product", Members: []string{"A"}},
	}
	l := accessLimits(viewer, rules)
	if !l.visible("Region", "East") || l.visible("Region", "West") || !l.visible("Product", "B") {
		t.Errorf("read limits: %+v", l)
	}
	if l["Region"].write["East"] {
		t.Error("a read-only rule must remove the write right")
	}
	if len(accessLimits(modeler, rules)) != 0 {
		t.Error("a MODELER must ignore the rules")
	}
}

func TestLimitsThroughProperties(t *testing.T) {
	em := engineModel{Dims: []engineDim{
		{Name: "Product", Members: []string{"A", "B"}},
		{Name: "Sales", Members: []string{"1", "2", "3"},
			Props: []engineProp{{Name: "Product", Target: "Product", Values: map[string]string{"1": "A", "2": "B"}}}},
		{Name: "Line", Members: []string{"x", "y"},
			Props: []engineProp{{Name: "Sale", Target: "Sales", Values: map[string]string{"x": "1", "y": "2"}}}},
	}}
	l := accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: "Product", Members: []string{"A"}, Write: true}}).through(em)
	if !l.visible("Sales", "1") || l.visible("Sales", "2") || l.visible("Sales", "3") {
		t.Errorf("rows of other or blank products must be hidden: %+v", l["Sales"])
	}
	if !l.visible("Line", "x") || l.visible("Line", "y") || !l["Line"].write["x"] {
		t.Errorf("the limit must follow a chain of properties: %+v", l["Line"])
	}
}

// TestAccessByRole needs PostgreSQL, but no engine: the interceptor refuses the calls before the engine.
func TestAccessByRole(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := "api-test-" + uuid.NewString()
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
