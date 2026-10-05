package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

// TestCreateAndUpdateItem needs PostgreSQL, but no engine: the item RPCs use only the api tables.
func TestCreateAndUpdateItem(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := "api-test-" + uuid.NewString()
	if _, err := pool.Exec(ctx, "insert into app_application (id, name) values ($1, 'test')", app); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "insert into app_member (app_id, user_name, role) values ($1, 'alice', 4)", app); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Exec(context.Background(), "delete from app_item where app_id = $1", app)
		pool.Exec(context.Background(), "delete from app_application where id = $1", app)
	})
	server := &PlanServer{Pool: pool, Engines: &Engines{}}
	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewPlanServiceHandler(server, connect.WithInterceptors(server.Interceptor())))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	client := nanashiv1connect.NewPlanServiceClient(srv.Client(), srv.URL)
	as := func(req interface{ Header() http.Header }) { req.Header().Set("X-Nanashi-User", "alice") }
	id := strings.ToUpper(uuid.Must(uuid.NewV7()).String())
	table := func(name string) *nanashiv1.TableDef { return &nanashiv1.TableDef{Id: id, Name: name} }
	create := func(op string) error {
		req := connect.NewRequest(&nanashiv1.CreateTableRequest{AppId: app, ClientOpId: op, Table: table("a")})
		as(req)
		_, err := client.CreateTable(ctx, req)
		return err
	}
	if err := create("not-a-uuid"); connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("bad client_op_id: got %v, want InvalidArgument", err)
	}
	if err := create(uuid.NewString()); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := create(uuid.NewString()); connect.CodeOf(err) != connect.CodeAlreadyExists {
		t.Errorf("second create: got %v, want AlreadyExists", err)
	}
	update := func(def *nanashiv1.TableDef) error {
		req := connect.NewRequest(&nanashiv1.UpdateTableRequest{AppId: app, ClientOpId: uuid.NewString(), Table: def})
		as(req)
		_, err := client.UpdateTable(ctx, req)
		return err
	}
	if err := update(table("b")); err != nil {
		t.Errorf("update: %v", err)
	}
	if err := update(&nanashiv1.TableDef{Id: uuid.NewString(), Name: "c"}); connect.CodeOf(err) != connect.CodeNotFound {
		t.Errorf("update of a missing id: got %v, want NotFound", err)
	}
	view := connect.NewRequest(&nanashiv1.UpdateViewRequest{AppId: app, ClientOpId: uuid.NewString(), View: &nanashiv1.ViewDef{Id: id}})
	as(view)
	if _, err := client.UpdateView(ctx, view); connect.CodeOf(err) != connect.CodeNotFound {
		t.Errorf("update of a table as a view: got %v, want NotFound", err)
	}
	var stored, name string
	if err := pool.QueryRow(ctx, "select id, def->>'name' from app_item where app_id = $1", app).Scan(&stored, &name); err != nil {
		t.Fatal(err)
	}
	if stored != strings.ToLower(id) || name != "b" {
		t.Errorf("stored item: got %s %q, want %s \"b\"", stored, name, strings.ToLower(id))
	}
}
