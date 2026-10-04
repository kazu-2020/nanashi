package router

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PgResolver reads the leader from the lease the engines keep in nanashi_model.
type PgResolver struct {
	pool *pgxpool.Pool
}

func NewPgResolver(ctx context.Context, dsn string) (*PgResolver, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return &PgResolver{pool: pool}, nil
}

func (p *PgResolver) Close() { p.pool.Close() }

// Ready returns an error if the engine schema (nanashi_model) is missing.
func (p *PgResolver) Ready(ctx context.Context) error {
	_, err := p.pool.Exec(ctx, "select 1 from nanashi_model limit 1")
	return err
}

func (p *PgResolver) Leader(ctx context.Context, model string) (string, error) {
	var endpoint *string
	var live *bool
	err := p.pool.QueryRow(ctx,
		"select lease_endpoint, lease_expires > now() from nanashi_model where model_id = $1", model,
	).Scan(&endpoint, &live)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", ErrUnknownModel
	}
	if err != nil || endpoint == nil || live == nil || !*live {
		return "", err
	}
	return *endpoint, nil
}

// Create adds the row of model. It starts no engine. It is idempotent.
func (p *PgResolver) Create(ctx context.Context, model string) error {
	_, err := p.pool.Exec(ctx, "insert into nanashi_model (model_id) values ($1) on conflict do nothing", model)
	return err
}
