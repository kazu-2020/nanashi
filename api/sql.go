package api

import (
	"context"

	"connectrpc.com/connect"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// arrowStream is the media type of an Arrow IPC stream (tessera/docs/sql.md).
const arrowStream = "application/vnd.apache.arrow.stream"

// The actions follow.

// QuerySql sends the SQL text to the engine without a change and gives the Arrow IPC stream.
//
//nolint:revive // nanashiv1connect gives the name.
func (s *PlanServer) QuerySql(ctx context.Context, req *connect.Request[nanashiv1.QuerySqlRequest]) (*connect.Response[nanashiv1.QuerySqlResponse], error) {
	b, err := s.post(ctx, req.Header().Get("X-Nanashi-User"), req.Msg.GetAppId(), "/sql", arrowStream,
		map[string]string{"sql": req.Msg.GetSql()})
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&nanashiv1.QuerySqlResponse{Arrow: b}), nil
}
