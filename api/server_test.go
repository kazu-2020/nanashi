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
	var migrated bool
	if err := pool.QueryRow(ctx, "select to_regclass('nanashi_model') is not null").Scan(&migrated); err != nil || !migrated {
		skip("nanashi_model is missing (python -m sparse_engine.pg_journal migrate): %v", err)
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

	res, err := listModels(t, pool)
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

// TestListModelsHidesDatabaseError needs no database. The pool points to a closed port.
func TestListModelsHidesDatabaseError(t *testing.T) {
	pool, err := pgxpool.New(context.Background(), "postgresql://nobody@127.0.0.1:1/secret_db?connect_timeout=1")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	_, err = listModels(t, pool)
	if connect.CodeOf(err) != connect.CodeUnavailable || strings.Contains(err.Error(), "secret_db") {
		t.Errorf("got %v, want unavailable without database details", err)
	}
}

// listModels sends the request through Connect, as the frontend does.
func listModels(t *testing.T, pool *pgxpool.Pool) (*connect.Response[nanashiv1.ListModelsResponse], error) {
	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewModelServiceHandler(&ModelServer{Pool: pool}))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return nanashiv1connect.NewModelServiceClient(srv.Client(), srv.URL).
		ListModels(context.Background(), connect.NewRequest(&nanashiv1.ListModelsRequest{}))
}
