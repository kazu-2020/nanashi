// Package api is the application server. It gives the Connect API to the frontend.
package api

import (
	"context"
	"errors"
	"log"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// ModelServer reads the models from nanashi_model in the engine journal.
type ModelServer struct {
	Pool *pgxpool.Pool
}

func (s *ModelServer) ListModels(ctx context.Context, _ *connect.Request[nanashiv1.ListModelsRequest]) (*connect.Response[nanashiv1.ListModelsResponse], error) {
	// CollectRows closes the rows and also returns the error of Query.
	rows, _ := s.Pool.Query(ctx,
		"select model_id, coalesce(lease_expires > now(), false) from nanashi_model order by model_id")
	models, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Model, error) {
		m := &nanashiv1.Model{}
		return m, row.Scan(&m.Id, &m.Open)
	})
	if err != nil {
		// Do not send the database error to the client. It can contain table names and addresses.
		log.Printf("ListModels: %v", err)
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("モデルの一覧を読めません"))
	}
	return connect.NewResponse(&nanashiv1.ListModelsResponse{Models: models}), nil
}
