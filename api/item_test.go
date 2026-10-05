package api

import (
	"context"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/uuid"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// TestCreateAndUpdateItem needs PostgreSQL, but no engine: the item RPCs use only the api tables.
func TestCreateAndUpdateItem(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pool := testPool(t, ctx)
	app := testApp(t, ctx, pool)
	alice := testClient(t, pool, nil)("alice")
	id := uuid.Must(uuid.NewV7()).String()
	table := func(name string) *nanashiv1.TableDef {
		return &nanashiv1.TableDef{Id: id, Name: name, Metrics: []string{budget}}
	}
	create := func(op string) error {
		_, err := alice.CreateTable(ctx, connect.NewRequest(&nanashiv1.CreateTableRequest{AppId: app, ClientOpId: op, Table: table("a")}))
		return err
	}
	if err := create("not-a-uuid"); connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("bad client_op_id: got %v, want InvalidArgument", err)
	}
	upper := &nanashiv1.CreateTableRequest{AppId: app, ClientOpId: uuid.NewString(), Table: &nanashiv1.TableDef{Id: strings.ToUpper(id), Name: "a"}}
	if _, err := alice.CreateTable(ctx, connect.NewRequest(upper)); connect.CodeOf(err) != connect.CodeInvalidArgument {
		t.Fatalf("upper case id: got %v, want InvalidArgument", err)
	}
	op := uuid.NewString()
	if err := create(op); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := create(op); err != nil {
		t.Errorf("resend of the create: got %v, want the stored result", err)
	}
	if err := create(uuid.NewString()); connect.CodeOf(err) != connect.CodeAlreadyExists {
		t.Errorf("second create: got %v, want AlreadyExists", err)
	}
	update := func(def *nanashiv1.TableDef) error {
		_, err := alice.UpdateTable(ctx, connect.NewRequest(&nanashiv1.UpdateTableRequest{AppId: app, ClientOpId: uuid.NewString(), Table: def}))
		return err
	}
	if err := update(table("b")); err != nil {
		t.Errorf("update: %v", err)
	}
	if err := update(&nanashiv1.TableDef{Id: uuid.NewString(), Name: "c"}); connect.CodeOf(err) != connect.CodeNotFound {
		t.Errorf("update of a missing id: got %v, want NotFound", err)
	}
	if _, err := alice.UpdateView(ctx, connect.NewRequest(&nanashiv1.UpdateViewRequest{AppId: app, ClientOpId: uuid.NewString(), View: &nanashiv1.ViewDef{Id: id}})); connect.CodeOf(err) != connect.CodeNotFound {
		t.Errorf("update of a table as a view: got %v, want NotFound", err)
	}
	var stored, name, metric string
	var version int64
	if err := pool.QueryRow(ctx, "select i.id, i.def->>'name', i.def->'metrics'->>0, a.version from app_item i join app_application a on a.id = i.app_id where i.app_id = $1", app).Scan(&stored, &name, &metric, &version); err != nil {
		t.Fatal(err)
	}
	if stored != id || name != "b" || metric != budget || version != 2 {
		t.Errorf("stored item: got %s %q %s version %d, want %s \"b\" %s version 2", stored, name, metric, version, id, budget)
	}
}
