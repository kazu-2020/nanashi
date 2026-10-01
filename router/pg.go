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
