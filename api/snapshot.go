package api

import (
	"context"
	"encoding/json"
	"errors"
	"strings"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func sameSeq(em engineModel, cubes ...map[string]engineCube) bool {
	for _, m := range cubes {
		for _, c := range m {
			if c.Seq != em.Seq {
				return false
			}
		}
	}
	return true
}

// snapshotData is the content of a snapshot. Engine is the body of GET / as the engine sent it.
// Inputs and Overrides are by Metric id. Overrides has the cells that override the formula of each overridable Metric.
type snapshotData struct {
	Engine    json.RawMessage               `json:"engine"`
	Inputs    map[string]engineCube         `json:"inputs"`
	Overrides map[string]engineCube         `json:"overrides,omitempty"`
	Kinds     map[string]nanashiv1.ListKind `json:"kinds"`
	Props     []propRow                     `json:"props"`
	Items     []itemRow                     `json:"items"`
	Metrics   map[string]metricRow          `json:"metrics,omitempty"` // The Metric catalog, by Metric id.
}

// replayOps makes the operations that build the engine model of a snapshot in an empty model, with the same ids.
// ponytail: formulas replay in the engine order.
func replayOps(em engineModel, inputs, overrides map[string]engineCube) []op {
	var ops []op
	for _, d := range em.Dims {
		ops = append(ops, newOp("add_dimension", map[string]any{"id": d.ID, "name": d.Name, "ordered": d.Ordered}))
		for _, m := range d.Members {
			ops = append(ops, newOp("add_member", map[string]any{"dim": d.ID, "id": m.ID, "name": m.Name}))
		}
	}
	for _, d := range em.Dims {
		for _, p := range d.Props {
			ops = append(ops, newOp("add_property", map[string]any{"dim": d.ID, "id": p.ID, "name": p.Name, "target": p.Target}),
				newOp("set_property_values", map[string]any{"dim": d.ID, "prop": p.ID, "values": p.Values}))
		}
	}
	for _, m := range em.Metrics {
		if m.Formula != "" {
			ops = append(ops, newOp("add_formula", map[string]any{"id": m.ID, "name": m.Name, "dims": m.Dims, "formula": m.Formula, "kind": m.Kind, "overridable": m.Overridable}))
			continue
		}
		cube := inputs[m.ID]
		index := make([]int, len(m.Dims))
		for i, d := range m.Dims {
			index[i] = slicesIndex(cube.Dims, d)
		}
		cells := [][]any{}
		for _, c := range cube.Cells {
			coords := make([]any, len(index))
			for i, j := range index {
				coords[i] = c[j]
			}
			cells = append(cells, []any{coords, c[len(c)-1]})
		}
		ops = append(ops, newOp("add_input", map[string]any{"id": m.ID, "name": m.Name, "dims": m.Dims, "kind": m.Kind, "cells": cells}))
	}
	for _, m := range em.Metrics {
		if cube, ok := overrides[m.ID]; ok {
			ops = append(ops, copyCellOps(m.ID, "", "", cube, true)...)
		}
	}
	return ops
}

func slicesIndex(s []string, x string) int {
	for i, v := range s {
		if v == x {
			return i
		}
	}
	return -1
}

// restoreStmts gives the statements that copy the api rows of a snapshot into the application app.
func restoreStmts(app string, snap snapshotData) []stmt {
	var out []stmt
	for list, kind := range snap.Kinds {
		out = append(out, stmt{sql: "insert into app_list (app_id, id, kind) values ($1, $2, $3)", args: []any{app, list, kind}})
	}
	for _, p := range snap.Props {
		var metric *string
		if p.MetricID != "" {
			metric = &p.MetricID
		}
		out = append(out, stmt{sql: "insert into app_property (app_id, list_id, id, name, type, metric_id, text_values) values ($1, $2, $3, $4, $5, $6, $7)",
			args: []any{app, p.ListID, p.ID, p.Name, p.Type, metric, textJSON(p.Text)}})
	}
	for id, c := range snap.Metrics {
		out = append(out, stmt{sql: "insert into app_metric (app_id, metric_id, description, folder, owner) values ($1, $2, $3, $4, $5)",
			args: []any{app, id, c.Description, c.Folder, c.Owner}})
	}
	for _, it := range snap.Items {
		out = append(out, stmt{sql: "insert into app_item (app_id, id, type, def) values ($1, $2, $3, $4)", args: []any{app, it.ID, it.Type, string(it.Def)}})
	}
	return out
}

// The actions follow.

// CreateApplication goes through the outbox: the first transaction makes the application and its member, the
// engine gets the model, and the application shows only when the row is done. A refusal deletes the application.
func (s *PlanServer) CreateApplication(ctx context.Context, req *connect.Request[nanashiv1.CreateApplicationRequest]) (*connect.Response[nanashiv1.Application], error) {
	c := callerOf(ctx)
	name := strings.TrimSpace(req.Msg.Name)
	if name == "" || req.Msg.Id == "" {
		return nil, invalid(errors.New("アプリケーションの id と名前が要る"))
	}
	app := req.Msg.Id
	_, err := s.outbox(ctx, app, req.Msg, func() (plan, error) {
		var snap snapshotData
		if req.Msg.SnapshotId != "" {
			var source, content string
			err := s.Pool.QueryRow(ctx, "select app_id, content from app_snapshot where id = $1", req.Msg.SnapshotId).Scan(&source, &content)
			if errors.Is(err, pgx.ErrNoRows) {
				return plan{}, connect.NewError(connect.CodeNotFound, errors.New("スナップショットがない"))
			}
			if err != nil {
				return plan{}, dbError(err)
			}
			// The new application has no access rules, so only a user who reads all data can restore it.
			r, _, err := s.rights(ctx, source, c.user)
			if err != nil {
				return plan{}, err
			}
			if r < modeler {
				return plan{}, connect.NewError(connect.CodePermissionDenied, errors.New("このスナップショットを戻す権限がない"))
			}
			if err := json.Unmarshal([]byte(content), &snap); err != nil {
				return plan{}, dbError(err)
			}
		}
		p := plan{
			stmts: []stmt{
				{sql: "insert into app_application (id, name) values ($1, $2) on conflict do nothing", args: []any{app, name}, zero: tag(errExists, "同じ id のアプリケーションがすでにある")},
				{sql: "insert into app_member (app_id, user_name, role) values ($1, $2, $3)", args: []any{app, c.user, admin}},
			},
			made: []madeRow{{Table: "app_application", ID: app}},
		}
		if snap.Engine == nil {
			return p, nil
		}
		em, err := parseEngineModel(snap.Engine)
		if err != nil {
			return plan{}, dbError(err)
		}
		p.stmts = append(p.stmts, restoreStmts(app, snap)...)
		p.ops = replayOps(em, snap.Inputs, snap.Overrides)
		return p, nil
	})
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&nanashiv1.Application{Id: app, Name: name, Role: admin}), nil
}

func (s *PlanServer) ListSnapshots(ctx context.Context, req *connect.Request[nanashiv1.ListSnapshotsRequest]) (*connect.Response[nanashiv1.ListSnapshotsResponse], error) {
	rows, _ := s.Pool.Query(ctx, `select id, name, user_name, (extract(epoch from created_at) * 1000)::bigint
		from app_snapshot where app_id = $1 order by created_at desc`, req.Msg.AppId)
	snaps, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Snapshot, error) {
		x := &nanashiv1.Snapshot{}
		return x, row.Scan(&x.Id, &x.Name, &x.User, &x.CreatedAt)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.ListSnapshotsResponse{Snapshots: snaps}), nil
}

// CreateSnapshot reads one version of the application without a lock. It settles the pending operations, reads
// the engine (all cubes of one seq), and then reads the api rows in one REPEATABLE READ transaction. If the
// version changed since the start, or an operation is pending, the api rows can differ from the engine: read again.
func (s *PlanServer) CreateSnapshot(ctx context.Context, req *connect.Request[nanashiv1.CreateSnapshotRequest]) (*connect.Response[nanashiv1.Snapshot], error) {
	app := req.Msg.AppId
	if req.Msg.Id == "" {
		return nil, invalid(errors.New("スナップショットの id が要る"))
	}
	snap := &nanashiv1.Snapshot{Id: req.Msg.Id, Name: req.Msg.Name, User: callerOf(ctx).user}
	for try := 0; ; try++ {
		if err := s.settlePending(ctx, app); err != nil {
			return nil, err
		}
		var version int64
		if err := s.Pool.QueryRow(ctx, "select version from app_application where id = $1", app).Scan(&version); err != nil {
			return nil, dbError(err)
		}
		data, err := s.readEngine(ctx, app)
		if err != nil {
			return nil, err
		}
		result, err := s.apiOnlyTx(ctx, app, req.Msg, pgx.TxOptions{IsoLevel: pgx.RepeatableRead}, func(tx pgx.Tx) (any, error) {
			var now, pending int64
			if err := tx.QueryRow(ctx, `select a.version, (select count(*) from app_operation o where o.app_id = a.id and o.status = 'pending')
				from app_application a where a.id = $1`, app).Scan(&now, &pending); err != nil {
				return nil, err
			}
			if now != version || pending > 0 {
				return nil, errChanged
			}
			meta, err := metaIn(ctx, tx, app)
			if err != nil {
				return nil, err
			}
			data.Kinds, data.Props, data.Metrics = meta.Kinds, meta.Props, meta.Metrics
			if data.Items, err = itemsIn(ctx, tx, app); err != nil {
				return nil, err
			}
			content, err := json.Marshal(data)
			if err != nil {
				return nil, err
			}
			var ms int64
			err = tx.QueryRow(ctx, `insert into app_snapshot (id, app_id, name, user_name, content) values ($1, $2, $3, $4, $5)
				returning (extract(epoch from created_at) * 1000)::bigint`, snap.Id, app, snap.Name, snap.User, string(content)).Scan(&ms)
			return map[string]int64{"created_at": ms}, err
		})
		if errors.Is(err, errChanged) {
			if try == 2 {
				return nil, connect.NewError(connect.CodeAborted, errors.New("モデルが変わり続けているので、スナップショットを作れない"))
			}
			continue
		}
		if err != nil {
			return nil, err
		}
		var stored struct {
			CreatedAt int64 `json:"created_at"`
		}
		json.Unmarshal(result, &stored)
		snap.CreatedAt = stored.CreatedAt
		return connect.NewResponse(snap), nil
	}
}

// errChanged passes dbError as a Connect error. CreateSnapshot reads again.
var errChanged = connect.NewError(connect.CodeAborted, errors.New("changed"))

// readEngine reads the model and the cells of one version of the engine.
func (s *PlanServer) readEngine(ctx context.Context, app string) (snapshotData, error) {
	var data snapshotData
	// A write between two reads gives cubes of different versions. Then read all again.
	for try := 0; ; try++ {
		em, raw, err := s.Engines.model(ctx, app)
		if err != nil {
			return data, err
		}
		data = snapshotData{Engine: raw, Inputs: map[string]engineCube{}, Overrides: map[string]engineCube{}}
		for _, m := range em.Metrics {
			if m.Formula == "" {
				if data.Inputs[m.ID], err = s.Engines.read(ctx, app, engineRead{Metric: m.ID, Path: "slice"}); err != nil {
					return data, err
				}
			} else if m.Overridable {
				if data.Overrides[m.ID], err = s.Engines.read(ctx, app, engineRead{Metric: m.ID, Path: "overrides"}); err != nil {
					return data, err
				}
			}
		}
		if sameSeq(em, data.Inputs, data.Overrides) {
			return data, nil
		}
		if try == 2 {
			return data, connect.NewError(connect.CodeAborted, errors.New("モデルが変わり続けているので、スナップショットを作れない"))
		}
	}
}
