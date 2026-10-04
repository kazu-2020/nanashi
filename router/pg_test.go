package router

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"
)

func TestPgResolver(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	dsn := cmp.Or(os.Getenv("NANASHI_PG_DSN"), "postgresql://postgres@127.0.0.1:55432/nanashi")
	p, err := NewPgResolver(ctx, dsn)
	if err != nil {
		t.Skipf("PostgreSQL (%s) is not reachable: %v", dsn, err)
	}
	t.Cleanup(p.Close)
	var migrated bool
	if err := p.pool.QueryRow(ctx, "select to_regclass('nanashi_model') is not null").Scan(&migrated); err != nil || !migrated {
		t.Skipf("nanashi_model is missing (python -m sparse_engine.pg_journal migrate): %v", err)
	}
	prefix := fmt.Sprintf("router-test-%d-", time.Now().UnixNano())
	rows := map[string]string{
		"live":     "now() + interval '30 seconds'",
		"expired":  "now() - interval '1 second'",
		"released": "null",
	}
	for name, expires := range rows {
		if _, err := p.pool.Exec(ctx, "insert into nanashi_model (model_id, lease_endpoint, lease_expires) values ($1, $2, "+expires+")",
			prefix+name, "http://"+name+":8080"); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := p.pool.Exec(ctx, "insert into nanashi_model (model_id) values ($1)", prefix+"never"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		p.pool.Exec(context.Background(), "delete from nanashi_model where model_id like $1", prefix+"%")
	})

	for model, want := range map[string]string{"live": "http://live:8080", "expired": "", "released": "", "never": ""} {
		got, err := p.Leader(ctx, prefix+model)
		if got != want || err != nil {
			t.Errorf("%s: got %q, %v; want %q", model, got, err, want)
		}
	}
	if _, err := p.Leader(ctx, prefix+"missing"); !errors.Is(err, ErrUnknownModel) {
		t.Errorf("missing: got %v, want ErrUnknownModel", err)
	}
}

func TestPgResolverCreate(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	dsn := cmp.Or(os.Getenv("NANASHI_PG_DSN"), "postgresql://postgres@127.0.0.1:55432/nanashi")
	p, err := NewPgResolver(ctx, dsn)
	if err != nil {
		t.Skipf("PostgreSQL (%s) is not reachable: %v", dsn, err)
	}
	t.Cleanup(p.Close)
	var migrated bool
	if err := p.pool.QueryRow(ctx, "select to_regclass('nanashi_model') is not null").Scan(&migrated); err != nil || !migrated {
		t.Skipf("nanashi_model is missing (python -m sparse_engine.pg_journal migrate): %v", err)
	}
	model := fmt.Sprintf("router-create-%d", time.Now().UnixNano())
	t.Cleanup(func() { p.pool.Exec(context.Background(), "delete from nanashi_model where model_id = $1", model) })
	if _, err := p.Leader(ctx, model); !errors.Is(err, ErrUnknownModel) {
		t.Fatalf("before Create: got %v, want ErrUnknownModel", err)
	}
	for range 2 {
		if err := p.Create(ctx, model); err != nil {
			t.Fatalf("Create: %v", err)
		}
	}
	if url, err := p.Leader(ctx, model); url != "" || err != nil {
		t.Errorf("after Create: got %q, %v; want no leader", url, err)
	}
}
