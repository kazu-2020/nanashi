package api

import (
	"context"
	"strconv"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// The actions follow.

func (s *PlanServer) ListAudit(ctx context.Context, req *connect.Request[nanashiv1.ListAuditRequest]) (*connect.Response[nanashiv1.ListAuditResponse], error) {
	limit := req.Msg.Limit
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	rows, _ := s.Pool.Query(ctx, `select id, user_name, (extract(epoch from created_at) * 1000)::bigint, action, detail
		from app_audit where app_id = $1 order by id desc limit $2`, req.Msg.AppId, limit)
	entries, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.AuditEntry, error) {
		e := &nanashiv1.AuditEntry{}
		var id int64
		err := row.Scan(&id, &e.User, &e.CreatedAt, &e.Action, &e.Detail)
		e.Id = strconv.FormatInt(id, 10)
		return e, err
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.ListAuditResponse{Entries: entries}), nil
}
