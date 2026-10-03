// Package api is the application server. It gives the Connect API to the frontend.
package api

import (
	"context"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5/pgxpool"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// ModelServer reads the models from nanashi_model in the engine journal.
type ModelServer struct {
	Pool *pgxpool.Pool
}

func (s *ModelServer) ListModels(ctx context.Context, _ *connect.Request[nanashiv1.ListModelsRequest]) (*connect.Response[nanashiv1.ListModelsResponse], error) {
	rows, err := s.Pool.Query(ctx,
		"select model_id, coalesce(lease_expires > now(), false) from nanashi_model order by model_id")
	if err != nil {
		return nil, err
	}
	res := &nanashiv1.ListModelsResponse{}
	for rows.Next() {
		m := &nanashiv1.Model{}
		if err := rows.Scan(&m.Id, &m.Open); err != nil {
			return nil, err
		}
		res.Models = append(res.Models, m)
	}
	return connect.NewResponse(res), rows.Err()
}
