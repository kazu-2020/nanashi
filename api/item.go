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

// itemDef is the definition of a table, a view or a board.
type itemDef interface {
	proto.Message
	GetId() string
}

// The actions follow.

func (s *PlanServer) items(ctx context.Context, app string) ([]itemRow, error) {
	return itemsIn(ctx, s.Pool, app)
}

func itemsIn(ctx context.Context, q querier, app string) ([]itemRow, error) {
	rows, _ := q.Query(ctx, "select type, id, def from app_item where app_id = $1 order by ord", app)
	items, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (itemRow, error) {
		var it itemRow
		return it, row.Scan(&it.Type, &it.ID, &it.Def)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return items, nil
}

func (s *PlanServer) CreateTable(ctx context.Context, req *connect.Request[nanashiv1.CreateTableRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, req.Msg, nanashiv1.ItemType_ITEM_TYPE_TABLE, req.Msg.Table, true)
}

func (s *PlanServer) UpdateTable(ctx context.Context, req *connect.Request[nanashiv1.UpdateTableRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, req.Msg, nanashiv1.ItemType_ITEM_TYPE_TABLE, req.Msg.Table, false)
}

func (s *PlanServer) CreateView(ctx context.Context, req *connect.Request[nanashiv1.CreateViewRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, req.Msg, nanashiv1.ItemType_ITEM_TYPE_VIEW, req.Msg.View, true)
}

func (s *PlanServer) UpdateView(ctx context.Context, req *connect.Request[nanashiv1.UpdateViewRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, req.Msg, nanashiv1.ItemType_ITEM_TYPE_VIEW, req.Msg.View, false)
}

func (s *PlanServer) CreateBoard(ctx context.Context, req *connect.Request[nanashiv1.CreateBoardRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, req.Msg, nanashiv1.ItemType_ITEM_TYPE_BOARD, req.Msg.Board, true)
}

func (s *PlanServer) UpdateBoard(ctx context.Context, req *connect.Request[nanashiv1.UpdateBoardRequest]) (*ack, error) {
	return s.saveItem(ctx, req.Msg.AppId, req.Msg, nanashiv1.ItemType_ITEM_TYPE_BOARD, req.Msg.Board, false)
}

// saveItem keeps the definition as protojson. GetModel takes the application ID and the id from the row, not from def.
// If create is true, an existing id gives AlreadyExists. If create is false, a missing id (of this type) gives NotFound.
func (s *PlanServer) saveItem(ctx context.Context, app string, req proto.Message, typ nanashiv1.ItemType, def itemDef, create bool) (*ack, error) {
	if def == nil || def.GetId() == "" {
		return nil, invalid(errors.New("id が要る"))
	}
	b, err := protojson.Marshal(def)
	if err != nil {
		return nil, err
	}
	st := stmt{"update app_item set def = $4 where app_id = $1 and id = $2 and type = $3",
		[]any{app, def.GetId(), typ, string(b)}, tag(errNotFound, "変更する対象がない")}
	if create {
		st.sql = "insert into app_item (app_id, id, type, def) values ($1, $2, $3, $4) on conflict (app_id, id) do nothing"
		st.zero = tag(errExists, "同じ id がすでにある")
	}
	return ackOf(s.apiOnly(ctx, app, req, pgx.TxOptions{}, func(tx pgx.Tx) (any, error) {
		err := execStmt(ctx, tx, st)
		if err == nil {
			_, err = tx.Exec(ctx, "update app_application set version = version + 1 where id = $1", app)
		}
		return nil, err
	}))
}

// DeleteItem deletes the row. GetModel leaves a deleted view out of the widgets of the boards.
func (s *PlanServer) DeleteItem(ctx context.Context, req *connect.Request[nanashiv1.DeleteItemRequest]) (*ack, error) {
	return ackOf(s.apiOnly(ctx, req.Msg.AppId, req.Msg, pgx.TxOptions{}, func(tx pgx.Tx) (any, error) {
		if _, err := tx.Exec(ctx, "delete from app_item where app_id = $1 and id = $2 and type = $3", req.Msg.AppId, req.Msg.Id, req.Msg.Type); err != nil {
			return nil, err
		}
		_, err := tx.Exec(ctx, "update app_application set version = version + 1 where id = $1", req.Msg.AppId)
		return nil, err
	}))
}
