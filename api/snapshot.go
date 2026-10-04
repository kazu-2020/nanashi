package api

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"slices"
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
// Overrides has the cells that override the formula of each overridable Metric.
type snapshotData struct {
	Engine    json.RawMessage               `json:"engine"`
	Inputs    map[string]engineCube         `json:"inputs"`
	Overrides map[string]engineCube         `json:"overrides,omitempty"`
	Kinds     map[string]nanashiv1.ListKind `json:"kinds"`
	Props     []propRow                     `json:"props"`
	Items     []itemRow                     `json:"items"`
}

// replayOps makes the operations that build the engine model of a snapshot in an empty model.
// ponytail: formulas replay in the engine order.
func replayOps(em engineModel, inputs, overrides map[string]engineCube) []op {
	var ops []op
	for _, d := range em.Dims {
		ops = append(ops, newOp("add_dimension", d.Name, d.Members).with(map[string]any{"ordered": d.Ordered}))
	}
	for _, d := range em.Dims {
		for _, p := range d.Props {
			ops = append(ops, newOp("add_property", d.Name, p.Name, p.Target, p.Values))
		}
	}
	for _, m := range em.Metrics {
		if m.Formula != "" {
			ops = append(ops, newOp("add_formula", m.Name, m.Dims, m.Formula).with(map[string]any{"kind": m.Kind, "overridable": m.Overridable}))
			continue
		}
		cube := inputs[m.Name]
		index := make([]int, len(m.Dims))
		for i, d := range m.Dims {
			index[i] = slices.Index(cube.Dims, d)
		}
		cells := [][]any{}
		for _, c := range cube.Cells {
			coords := make([]any, len(index))
			for i, j := range index {
				coords[i] = c[j]
			}
			cells = append(cells, []any{coords, c[len(c)-1]})
		}
		ops = append(ops, newOp("add_input", m.Name, m.Dims, cells).with(map[string]any{"kind": m.Kind}))
	}
	for _, m := range em.Metrics {
		if cube, ok := overrides[m.Name]; ok {
			ops = append(ops, copyCellOps(m.Name, "", "", cube)...)
		}
	}
	return ops
}

// The actions follow.

func (s *PlanServer) CreateApplication(ctx context.Context, req *connect.Request[nanashiv1.CreateApplicationRequest]) (*connect.Response[nanashiv1.Application], error) {
	c := callerOf(ctx)
	name := strings.TrimSpace(req.Msg.Name)
	if name == "" {
		return nil, invalid(errors.New("アプリケーションの名前が空"))
	}
	var snap snapshotData
	if req.Msg.SnapshotId != "" {
		var source, content string
		err := s.Pool.QueryRow(ctx, "select app_id, content from app_snapshot where id = $1", req.Msg.SnapshotId).Scan(&source, &content)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, connect.NewError(connect.CodeNotFound, errors.New("スナップショットがない"))
		}
		if err != nil {
			return nil, dbError(err)
		}
		// The new application has no access rules, so only a user who reads all data can restore it.
		r, _, err := s.rights(ctx, source, c.user)
		if err != nil {
			return nil, err
		}
		if r < modeler {
			return nil, connect.NewError(connect.CodePermissionDenied, errors.New("このスナップショットを戻す権限がない"))
		}
		if err := json.Unmarshal([]byte(content), &snap); err != nil {
			return nil, dbError(err)
		}
	}
	var em engineModel
	if snap.Engine != nil {
		var err error
		if em, err = parseEngineModel(snap.Engine); err != nil {
			return nil, dbError(err)
		}
	}
	app := "app-" + newID()
	// The rows go first, so an error before Engines.Create makes no engine model. A replay error or a commit error
	// after Engines.Create leaves an engine model without an application in the router (the router has no delete).
	err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "insert into app_application (id, name) values ($1, $2)", app, name); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "insert into app_member (app_id, user_name, role) values ($1, $2, $3)", app, c.user, admin); err != nil {
			return err
		}
		for list, kind := range snap.Kinds {
			if _, err := tx.Exec(ctx, "insert into app_list (app_id, name, kind) values ($1, $2, $3)", app, list, kind); err != nil {
				return err
			}
		}
		for _, p := range snap.Props {
			if _, err := tx.Exec(ctx, "insert into app_property (app_id, list, name, type, text_values) values ($1, $2, $3, $4, $5)",
				app, p.List, p.Name, p.Type, textJSON(p.Text)); err != nil {
				return err
			}
		}
		for _, it := range snap.Items {
			if _, err := tx.Exec(ctx, "insert into app_item (app_id, id, type, def) values ($1, $2, $3, $4)", app, it.ID, it.Type, string(it.Def)); err != nil {
				return err
			}
		}
		if err := s.Engines.Create(ctx, app); err != nil {
			log.Printf("CreateApplication: %v", err)
			return connect.NewError(connect.CodeUnavailable, errors.New("モデルを作れない"))
		}
		if snap.Engine == nil {
			return nil
		}
		return s.Engines.write(ctx, app, c.user, replayOps(em, snap.Inputs, snap.Overrides))
	})
	if err != nil {
		return nil, dbError(err)
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

func (s *PlanServer) CreateSnapshot(ctx context.Context, req *connect.Request[nanashiv1.CreateSnapshotRequest]) (*connect.Response[nanashiv1.Snapshot], error) {
	app := req.Msg.AppId
	// The lock makes the engine reads and the api rows (meta, items) read one version of the application.
	defer s.lock(app)()
	var data snapshotData
	// A write of a different api process between two reads gives cubes of different versions. Then read all again.
	for try := 0; ; try++ {
		em, raw, err := s.Engines.model(ctx, app)
		if err != nil {
			return nil, err
		}
		data = snapshotData{Engine: raw, Inputs: map[string]engineCube{}, Overrides: map[string]engineCube{}}
		for _, m := range em.Metrics {
			if m.Formula == "" {
				if data.Inputs[m.Name], err = s.Engines.read(ctx, app, engineRead{Metric: m.Name, Path: "slice"}); err != nil {
					return nil, err
				}
			} else if m.Overridable {
				if data.Overrides[m.Name], err = s.Engines.read(ctx, app, engineRead{Metric: m.Name, Path: "overrides"}); err != nil {
					return nil, err
				}
			}
		}
		if sameSeq(em, data.Inputs, data.Overrides) {
			break
		}
		if try == 2 {
			return nil, connect.NewError(connect.CodeAborted, errors.New("モデルが変わり続けているので、スナップショットを作れない"))
		}
	}
	meta, err := s.meta(ctx, app)
	if err != nil {
		return nil, err
	}
	data.Kinds, data.Props = meta.Kinds, meta.Props
	if data.Items, err = s.items(ctx, app); err != nil {
		return nil, err
	}
	content, err := json.Marshal(data)
	if err != nil {
		return nil, err
	}
	snap := &nanashiv1.Snapshot{Id: newID(), Name: req.Msg.Name, User: callerOf(ctx).user}
	if err := s.Pool.QueryRow(ctx, `insert into app_snapshot (id, app_id, name, user_name, content) values ($1, $2, $3, $4, $5)
		returning (extract(epoch from created_at) * 1000)::bigint`, snap.Id, app, snap.Name, snap.User, string(content)).Scan(&snap.CreatedAt); err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(snap), nil
}
