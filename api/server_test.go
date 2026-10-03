package api

import (
	"cmp"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5/pgxpool"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

func TestListModels(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	dsn := cmp.Or(os.Getenv("NANASHI_PG_DSN"), "postgresql://postgres@127.0.0.1:55432/nanashi")
	pool, err := pgxpool.New(ctx, dsn)
	if err == nil {
		err = pool.Ping(ctx)
	}
	if err != nil {
		t.Skipf("PostgreSQL (%s) is not reachable: %v", dsn, err)
	}
	t.Cleanup(pool.Close)
	var migrated bool
	if err := pool.QueryRow(ctx, "select to_regclass('nanashi_model') is not null").Scan(&migrated); err != nil || !migrated {
		t.Skipf("nanashi_model is missing (python -m sparse_engine.pg_journal migrate): %v", err)
	}
	prefix := fmt.Sprintf("api-test-%d-", time.Now().UnixNano())
	if _, err := pool.Exec(ctx, `insert into nanashi_model (model_id, lease_expires) values
		($1, now() + interval '30 seconds'), ($2, now() - interval '1 second'), ($3, null)`,
		prefix+"a-live", prefix+"b-expired", prefix+"c-released"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Exec(context.Background(), "delete from nanashi_model where model_id like $1", prefix+"%")
	})

	// Send the request through Connect, as the frontend does.
	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewModelServiceHandler(&ModelServer{Pool: pool}))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	res, err := nanashiv1connect.NewModelServiceClient(srv.Client(), srv.URL).
		ListModels(ctx, connect.NewRequest(&nanashiv1.ListModelsRequest{}))
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, m := range res.Msg.Models {
		if id, ok := strings.CutPrefix(m.Id, prefix); ok {
			got = append(got, fmt.Sprintf("%s=%v", id, m.Open))
		}
	}
	if want := "a-live=true b-expired=false c-released=false"; strings.Join(got, " ") != want {
		t.Errorf("got %q, want %q", strings.Join(got, " "), want)
	}
}
