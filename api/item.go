package api

import (
	"context"
	"encoding/json"

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

// saveItem keeps the definition as protojson. If *id is empty, it sets a new id (id points into msg).
func (s *PlanServer) saveItem(ctx context.Context, app string, typ nanashiv1.ItemType, id *string, msg proto.Message) error {
	if *id == "" {
		*id = newID()
	}
	def, err := protojson.Marshal(msg)
	if err != nil {
		return err
	}
	if _, err := s.Pool.Exec(ctx, `insert into app_item (app_id, id, type, def) values ($1, $2, $3, $4)
		on conflict (app_id, id) do update set def = excluded.def where app_item.type = excluded.type`, app, *id, typ, string(def)); err != nil {
		return dbError(err)
	}
	return nil
}

func (s *PlanServer) SaveTable(ctx context.Context, req *connect.Request[nanashiv1.TableDef]) (*connect.Response[nanashiv1.TableDef], error) {
	if err := s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_TABLE, &req.Msg.Id, req.Msg); err != nil {
		return nil, err
	}
	return connect.NewResponse(req.Msg), nil
}

func (s *PlanServer) SaveView(ctx context.Context, req *connect.Request[nanashiv1.ViewDef]) (*connect.Response[nanashiv1.ViewDef], error) {
	if err := s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_VIEW, &req.Msg.Id, req.Msg); err != nil {
		return nil, err
	}
	return connect.NewResponse(req.Msg), nil
}

func (s *PlanServer) SaveBoard(ctx context.Context, req *connect.Request[nanashiv1.BoardDef]) (*connect.Response[nanashiv1.BoardDef], error) {
	if err := s.saveItem(ctx, req.Msg.AppId, nanashiv1.ItemType_ITEM_TYPE_BOARD, &req.Msg.Id, req.Msg); err != nil {
		return nil, err
	}
	return connect.NewResponse(req.Msg), nil
}

func (s *PlanServer) DeleteItem(ctx context.Context, req *connect.Request[nanashiv1.DeleteItemRequest]) (*ack, error) {
	if _, err := s.Pool.Exec(ctx, "delete from app_item where app_id = $1 and id = $2 and type = $3",
		req.Msg.AppId, req.Msg.Id, req.Msg.Type); err != nil {
		return nil, dbError(err)
	}
	if req.Msg.Type == nanashiv1.ItemType_ITEM_TYPE_VIEW {
		// A board must not keep a widget of a deleted view.
		if _, err := s.Pool.Exec(ctx, `update app_item set def = jsonb_set(def, '{widgets}', coalesce(
			(select jsonb_agg(w) from jsonb_array_elements(def->'widgets') w where w->>'viewId' is distinct from $2), '[]'))
			where app_id = $1 and type = $3 and def->'widgets' @> jsonb_build_array(jsonb_build_object('viewId', $2::text))`,
			req.Msg.AppId, req.Msg.Id, nanashiv1.ItemType_ITEM_TYPE_BOARD); err != nil {
			return nil, dbError(err)
		}
	}
	return ok()
}
