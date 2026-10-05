package api

import (
	"context"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestAccessLimits(t *testing.T) {
	em := model(t)
	rules := []*nanashiv1.AccessRule{
		{Role: viewer, List: region, Members: []string{east, west}, Write: true},
		{Role: viewer, List: region, Members: []string{east}},
		{Role: contributor, List: product, Members: []string{memberA}},
		{Role: viewer, List: newMember, Members: []string{memberA}}, // An unknown list: the rule is inactive.
	}
	l := accessLimits(viewer, rules, em)
	if !l.visible(region, east) || l.visible(region, west) || !l.visible(product, memberB) || len(l) != 1 {
		t.Errorf("read limits: %+v", l)
	}
	if l[region].write[east] {
		t.Error("a read-only rule must remove the write right")
	}
	if len(accessLimits(modeler, rules, em)) != 0 {
		t.Error("a MODELER must ignore the rules")
	}
	// A rule with only unknown members shows nothing (fail-closed).
	gone := accessLimits(viewer, []*nanashiv1.AccessRule{{Role: viewer, List: region, Members: []string{newMember}}}, em)
	if gone.visible(region, east) || gone.visible(region, newMember) {
		t.Errorf("a rule of removed members must show nothing: %+v", gone)
	}
}

func TestLimitsThroughProperties(t *testing.T) {
	a, b, s1, s2, s3, x, y := "0192f3a4-0000-7000-8000-0000000000f1", "0192f3a4-0000-7000-8000-0000000000f2", "0192f3a4-0000-7000-8000-0000000000f3",
		"0192f3a4-0000-7000-8000-0000000000f4", "0192f3a4-0000-7000-8000-0000000000f5", "0192f3a4-0000-7000-8000-0000000000f6", "0192f3a4-0000-7000-8000-0000000000f7"
	line := "0192f3a4-0000-7000-8000-0000000000f8"
	em := engineModel{Dims: []engineDim{
		{ID: product, Name: "Product", Members: []engineMember{{a, "A"}, {b, "B"}}},
		{ID: sales, Name: "Sales", Members: []engineMember{{s1, "1"}, {s2, "2"}, {s3, "3"}},
			Props: []engineProp{{ID: "p1", Name: "Product", Target: product, Values: map[string]string{s1: a, s2: b}}}},
		{ID: line, Name: "Line", Members: []engineMember{{x, "x"}, {y, "y"}},
			Props: []engineProp{{ID: "p2", Name: "Sale", Target: sales, Values: map[string]string{x: s1, y: s2}}}},
	}}
	l := accessLimits(contributor, []*nanashiv1.AccessRule{{Role: contributor, List: product, Members: []string{a}, Write: true}}, em).through(em)
	if !l.visible(sales, s1) || l.visible(sales, s2) || l.visible(sales, s3) {
		t.Errorf("rows of other or blank products must be hidden: %+v", l[sales])
	}
	if !l.visible(line, x) || l.visible(line, y) || !l[line].write[x] {
		t.Errorf("the limit must follow a chain of properties: %+v", l[line])
	}
}

// TestAccessByRole needs PostgreSQL, but no engine: the interceptor refuses the calls before the engine.
func TestAccessByRole(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	// An application whose creation is pending is not shown.
	hidden := uuid.Must(uuid.NewV7()).String()
	for _, sql := range []string{
		"insert into app_application (id, name) values ($1, 'pending')",
		"insert into app_member (app_id, user_name, role) values ($1, 'alice', 4)",
		"insert into app_operation (app_id, client_op_id, user_name, method, request_hash, status) values ($1, $1, 'alice', 'CreateApplication', '', 'pending')",
	} {
		if _, err := pool.Exec(ctx, sql, hidden); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		pool.Exec(context.Background(), "delete from app_application where id = $1", hidden)
		pool.Exec(context.Background(), "delete from app_operation where app_id = $1", hidden)
	})
	client := testClient(t, pool, nil)
	list := func(user string) (map[string]nanashiv1.Role, error) {
		res, err := client(user).ListApplications(ctx, connect.NewRequest(&nanashiv1.ListApplicationsRequest{}))
		if err != nil {
			return nil, err
		}
		out := map[string]nanashiv1.Role{}
		for _, a := range res.Msg.Applications {
			out[a.Id] = a.Role
		}
		return out, nil
	}
	if _, err := client("").ListApplications(ctx, connect.NewRequest(&nanashiv1.ListApplicationsRequest{})); connect.CodeOf(err) != connect.CodeUnauthenticated {
		t.Errorf("no user: got %v, want unauthenticated", err)
	}
	for user, want := range map[string]nanashiv1.Role{"alice": admin, "bob": viewer, "carol": 0} {
		got, err := list(user)
		if err != nil || got[app] != want {
			t.Errorf("%s: got role %v (%v), want %v", user, got[app], err, want)
		}
		if _, ok := got[hidden]; ok {
			t.Errorf("%s sees the pending application", user)
		}
	}
	if _, err := client("bob").WriteCells(ctx, connect.NewRequest(&nanashiv1.WriteCellsRequest{AppId: app, ClientOpId: uuid.NewString()})); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("viewer writes: got %v, want permission denied", err)
	}
	if _, err := client("alice").GetModel(ctx, connect.NewRequest(&nanashiv1.GetModelRequest{AppId: hidden})); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("read of a pending application: got %v, want permission denied", err)
	}
	// The audit detail has the cell values of the requests, so the access rules of a VIEWER do not apply to it.
	if _, err := client("bob").ListAudit(ctx, connect.NewRequest(&nanashiv1.ListAuditRequest{AppId: app})); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("viewer reads the audit trail: got %v, want permission denied", err)
	}
	if _, err := client("carol").GetModel(ctx, connect.NewRequest(&nanashiv1.GetModelRequest{AppId: app})); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("non-member reads: got %v, want permission denied", err)
	}
	if _, err := client("alice").GetModel(ctx, connect.NewRequest(&nanashiv1.GetModelRequest{AppId: "not-an-id"})); connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Errorf("a bad app_id: got %v, want InvalidArgument", err)
	}
}
