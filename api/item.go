package api

import (
	"context"
	"encoding/json"
	"errors"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

type itemRow struct {
	Type nanashiv1.ItemType `json:"type"`
	ID   string             `json:"id"`
	Def  json.RawMessage    `json:"def"`
}

// The actions follow.

func (s *PlanServer) items(ctx context.Context, app string) ([]itemRow, error) {
	rows, _ := s.Pool.Query(ctx, "select type, id, def from app_item where app_id = $1 order by ord", app)
	items, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (itemRow, error) {
		var it itemRow
		return it, row.Scan(&it.Type, &it.ID, &it.Def)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return items, nil
}

// itemDef is the definition of a table, a view or a board.
type itemDef interface {
	proto.Message
	GetId() string
}

func (s *PlanServer) CreateTable(ctx context.Context, req *connect.Request[nanashiv1.CreateTableRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_TABLE, req.Msg.Table, true)
}

func (s *PlanServer) UpdateTable(ctx context.Context, req *connect.Request[nanashiv1.UpdateTableRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_TABLE, req.Msg.Table, false)
}

func (s *PlanServer) CreateView(ctx context.Context, req *connect.Request[nanashiv1.CreateViewRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_VIEW, req.Msg.View, true)
}

func (s *PlanServer) UpdateView(ctx context.Context, req *connect.Request[nanashiv1.UpdateViewRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_VIEW, req.Msg.View, false)
}

func (s *PlanServer) CreateBoard(ctx context.Context, req *connect.Request[nanashiv1.CreateBoardRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_BOARD, req.Msg.Board, true)
}

func (s *PlanServer) UpdateBoard(ctx context.Context, req *connect.Request[nanashiv1.UpdateBoardRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_BOARD, req.Msg.Board, false)
}

// saveItem keeps the definition as protojson. GetModel takes the application ID and the id from the row, not from def.
// If create is true, an existing id gives AlreadyExists. If create is false, a missing id (of this type) gives NotFound.
func (s *PlanServer) saveItem(ctx context.Context, app string, typ nanashiv1.ItemType, def itemDef, create bool) (*ack, error) {
	id, err := parseID("id", def.GetId())
	if err != nil {
		return nil, err
	}
	b, err := protojson.Marshal(def)
	if err != nil {
		return nil, err
	}
	// The lock keeps an item write out of a snapshot and out of a rename of the names that the item keeps.
	defer s.lock(app)()
	sql := "update app_item set def = $4 where app_id = $1 and id = $2 and type = $3"
	if create {
		sql = "insert into app_item (app_id, id, type, def) values ($1, $2, $3, $4) on conflict (app_id, id) do nothing"
	}
	res, err := s.Pool.Exec(ctx, sql, app, id, typ, string(b))
	if err != nil {
		return nil, dbError(err)
	}
	if res.RowsAffected() == 0 && create {
		return nil, connect.NewError(connect.CodeAlreadyExists, errors.New("同じ id がすでにある"))
	}
	if res.RowsAffected() == 0 {
		return nil, connect.NewError(connect.CodeNotFound, errors.New("変更する対象がない"))
	}
	return ok()
}

func (s *PlanServer) DeleteItem(ctx context.Context, req *connect.Request[nanashiv1.DeleteItemRequest]) (*ack, error) {
	id, err := parseID("id", req.Msg.Id)
	if err != nil {
		return nil, err
	}
	defer s.lock(req.Msg.AppId)()
	if _, err := s.Pool.Exec(ctx, "delete from app_item where app_id = $1 and id = $2 and type = $3",
		req.Msg.AppId, id, req.Msg.Type); err != nil {
		return nil, dbError(err)
	}
	if req.Msg.Type == nanashiv1.ItemType_ITEM_TYPE_VIEW {
		// A board must not keep a widget of a deleted view.
		if _, err := s.Pool.Exec(ctx, `update app_item set def = jsonb_set(def, '{widgets}', coalesce(
			(select jsonb_agg(w) from jsonb_array_elements(def->'widgets') w where w->>'viewId' is distinct from $2), '[]'))
			where app_id = $1 and type = $3 and def->'widgets' @> jsonb_build_array(jsonb_build_object('viewId', $2::text))`,
			req.Msg.AppId, id, nanashiv1.ItemType_ITEM_TYPE_BOARD); err != nil {
			return nil, dbError(err)
		}
	}
	return ok()
}
