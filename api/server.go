// Package api is the application server. It gives the Connect API (nanashi.v1.PlanService) to the frontend.
package api

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"path"
	"slices"
	"strconv"
	"strings"
	"sync"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

//go:embed schema.sql
var schema string

// Migrate makes the api tables (prefix app_) if they are missing.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx, schema)
	return err
}

// PlanServer implements PlanService. It keeps its own data in PostgreSQL and the model in the engines.
type PlanServer struct {
	Pool    *pgxpool.Pool
	Engines *Engines
	locks   sync.Map // Application ID to *sync.Mutex.
}

// lock makes the read-modify-write steps of one application run one at a time. Call the result to unlock.
// ponytail: the lock is in one api process. If a second api process runs, change only this body to
// pg_advisory_xact_lock(<2-key>, hashtext(app)) in a transaction on a dedicated connection outside the pool.
// The 2-key form does not collide with the 1-key locks of tessera (docs/engine-lifecycle.md).
func (s *PlanServer) lock(app string) func() {
	v, _ := s.locks.LoadOrStore(app, &sync.Mutex{})
	mu := v.(*sync.Mutex)
	mu.Lock()
	return mu.Unlock
}

var _ nanashiv1connect.PlanServiceHandler = (*PlanServer)(nil)

type (
	ack     = connect.Response[nanashiv1.Ack]
	role    = nanashiv1.Role
	rpcRule struct {
		min   role // The minimum role in the application. ROLE_UNSPECIFIED: the RPC needs no application.
		audit bool // Record the call in the audit trail.
	}
)

const (
	viewer      = nanashiv1.Role_ROLE_VIEWER
	contributor = nanashiv1.Role_ROLE_CONTRIBUTOR
	modeler     = nanashiv1.Role_ROLE_MODELER
	admin       = nanashiv1.Role_ROLE_ADMIN
)

// rpcRules gives the minimum role and the audit of each RPC. An RPC that is not in the table is refused.
var rpcRules = map[string]rpcRule{
	"ListApplications": {}, "CreateApplication": {audit: true},
	"GetModel": {min: viewer}, "Query": {min: viewer}, "ListComments": {min: viewer}, "AddComment": {viewer, true},
	"ListSnapshots": {min: viewer},
	// The audit detail has the full requests (cells and CSV), and the access rules do not apply to it.
	"ListAudit":  {min: modeler},
	"WriteCells": {contributor, true}, "Import": {contributor, true}, "CreateSnapshot": {contributor, true},
	"CreateList": {modeler, true}, "AddProperty": {modeler, true}, "EditMembers": {modeler, true},
	"CreateCalendar": {modeler, true}, "CreateScenario": {modeler, true}, "SaveMetric": {modeler, true},
	"RenameMetric": {modeler, true}, "DeleteMetric": {modeler, true}, "SaveTable": {modeler, true},
	"SaveView": {modeler, true}, "SaveBoard": {modeler, true}, "DeleteItem": {modeler, true},
	"GetAccess": {min: admin}, "SetMemberRole": {admin, true}, "SaveAccessRule": {admin, true},
	"DeleteAccessRule": {admin, true},
}

// caller is the user of a request and the rights of the user in the application of the request.
type caller struct {
	user  string
	role  role
	rules []*nanashiv1.AccessRule // The access rules of the application. Empty when role >= MODELER.
}

// limitsIn gives the limits of the caller in the model em.
func (c caller) limitsIn(em engineModel) limits { return accessLimits(c.role, c.rules).through(em) }

type callerKey struct{}

func callerOf(ctx context.Context) caller { return ctx.Value(callerKey{}).(caller) }

// Interceptor identifies the user, checks the role for the RPC, and records the audit trail.
func (s *PlanServer) Interceptor() connect.UnaryInterceptorFunc {
	return func(next connect.UnaryFunc) connect.UnaryFunc {
		return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
			// The API trusts this header only from this host or from a --trusted-proxy (cmd/nanashi-api).
			user := req.Header().Get("X-Nanashi-User")
			if user == "" {
				return nil, connect.NewError(connect.CodeUnauthenticated, errors.New("X-Nanashi-User がない"))
			}
			method := path.Base(req.Spec().Procedure)
			rule, ok := rpcRules[method]
			if !ok {
				return nil, connect.NewError(connect.CodeUnimplemented, fmt.Errorf("%s は使えない", method))
			}
			c := caller{user: user}
			appID := ""
			if a, ok := req.Any().(interface{ GetAppId() string }); ok {
				appID = a.GetAppId()
			}
			if rule.min != nanashiv1.Role_ROLE_UNSPECIFIED {
				var err error
				if c.role, c.rules, err = s.rights(ctx, appID, user); err != nil {
					return nil, err
				}
				if c.role < rule.min {
					return nil, connect.NewError(connect.CodePermissionDenied, errors.New("この操作をする権限がない"))
				}
			}
			res, err := next(context.WithValue(ctx, callerKey{}, c), req)
			if err == nil && rule.audit {
				if a, ok := res.Any().(*nanashiv1.Application); ok {
					appID = a.Id
				}
				detail, _ := protojson.Marshal(req.Any().(proto.Message))
				if _, err := s.Pool.Exec(ctx, "insert into app_audit (app_id, user_name, action, detail) values ($1, $2, $3, $4)",
					appID, user, method, string(detail)); err != nil {
					log.Printf("audit %s %s: %v", appID, method, err)
				}
			}
			return res, err
		}
	}
}

// rights gives the role of the user in the application and, for a role under MODELER, the access rules.
func (s *PlanServer) rights(ctx context.Context, app, user string) (role, []*nanashiv1.AccessRule, error) {
	var r int32
	err := s.Pool.QueryRow(ctx, "select role from app_member where app_id = $1 and user_name = $2", app, user).Scan(&r)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, nil, nil
	}
	if err != nil {
		return 0, nil, dbError(err)
	}
	if role(r) >= modeler {
		return role(r), nil, nil
	}
	rules, err := s.rules(ctx, app)
	return role(r), rules, err
}

// dbError hides the database error from the client. It can contain table names and addresses.
func dbError(err error) error {
	log.Printf("database: %v", err)
	return connect.NewError(connect.CodeUnavailable, errors.New("データベースを読み書きできない"))
}

func invalid(err error) error { return connect.NewError(connect.CodeInvalidArgument, err) }

// connectError gives a plan error its Connect code. A *connect.Error passes through, a tagged error gets the
// code of its tag, and any other error is an input error.
func connectError(err error) error {
	if cerr := new(connect.Error); errors.As(err, &cerr) {
		return err
	}
	for sentinel, code := range errorCodes {
		if errors.Is(err, sentinel) {
			return connect.NewError(code, err)
		}
	}
	return connect.NewError(connect.CodeInvalidArgument, err)
}

var errorCodes = map[error]connect.Code{
	errDenied:       connect.CodePermissionDenied,
	errExists:       connect.CodeAlreadyExists,
	errPrecondition: connect.CodeFailedPrecondition,
}

func ok() (*ack, error) { return connect.NewResponse(&nanashiv1.Ack{}), nil }

func (s *PlanServer) ListApplications(ctx context.Context, _ *connect.Request[nanashiv1.ListApplicationsRequest]) (*connect.Response[nanashiv1.ListApplicationsResponse], error) {
	rows, _ := s.Pool.Query(ctx, `select a.id, a.name, m.role from app_application a
		join app_member m on m.app_id = a.id where m.user_name = $1 order by a.created_at, a.id`, callerOf(ctx).user)
	apps, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Application, error) {
		a := &nanashiv1.Application{}
		return a, row.Scan(&a.Id, &a.Name, &a.Role)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.ListApplicationsResponse{Applications: apps}), nil
}

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
	app := "app-" + newID()
	if err := s.Engines.Create(ctx, app); err != nil {
		log.Printf("CreateApplication: %v", err)
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("モデルを作れない"))
	}
	if snap.Engine != nil {
		em, err := parseEngineModel(snap.Engine)
		if err != nil {
			return nil, dbError(err)
		}
		if err := s.Engines.write(ctx, app, c.user, replayOps(em, snap.Inputs, snap.Overrides)); err != nil {
			return nil, err
		}
	}
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
		return nil
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.Application{Id: app, Name: name, Role: admin}), nil
}

func textJSON(m map[string]string) string {
	if m == nil {
		m = map[string]string{}
	}
	b, _ := json.Marshal(m)
	return string(b)
}

func (s *PlanServer) meta(ctx context.Context, app string) (appMeta, error) {
	meta := appMeta{Kinds: map[string]nanashiv1.ListKind{}}
	rows, _ := s.Pool.Query(ctx, "select name, kind from app_list where app_id = $1", app)
	var name string
	var kind int32
	if _, err := pgx.ForEachRow(rows, []any{&name, &kind}, func() error {
		meta.Kinds[name] = nanashiv1.ListKind(kind)
		return nil
	}); err != nil {
		return meta, dbError(err)
	}
	rows, _ = s.Pool.Query(ctx, "select list, name, type, text_values from app_property where app_id = $1 order by ord", app)
	props, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (propRow, error) {
		var p propRow
		return p, row.Scan(&p.List, &p.Name, &p.Type, &p.Text)
	})
	if err != nil {
		return meta, dbError(err)
	}
	meta.Props = props
	return meta, nil
}

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

func (s *PlanServer) GetModel(ctx context.Context, req *connect.Request[nanashiv1.GetModelRequest]) (*connect.Response[nanashiv1.ModelDef], error) {
	app, c := req.Msg.AppId, callerOf(ctx)
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	meta, err := s.meta(ctx, app)
	if err != nil {
		return nil, err
	}
	propCells := map[string]engineCube{}
	for _, p := range meta.Props {
		if p.Type != nanashiv1.PropertyType_PROPERTY_TYPE_TEXT {
			name := propMetric(p.List, p.Name)
			if propCells[name], err = s.Engines.read(ctx, app, engineRead{Metric: name, Path: "slice"}); err != nil {
				return nil, err
			}
		}
	}
	out := &nanashiv1.ModelDef{Role: c.role}
	out.Lists, out.Metrics = modelDef(em, meta, propCells, c.limitsIn(em))
	items, err := s.items(ctx, app)
	if err != nil {
		return nil, err
	}
	for _, it := range items {
		// The definition can come from a snapshot of another application, so set app_id and id again.
		switch it.Type {
		case nanashiv1.ItemType_ITEM_TYPE_TABLE:
			t := &nanashiv1.TableDef{}
			err = protojson.Unmarshal(it.Def, t)
			t.AppId, t.Id = app, it.ID
			out.Tables = append(out.Tables, t)
		case nanashiv1.ItemType_ITEM_TYPE_VIEW:
			v := &nanashiv1.ViewDef{}
			err = protojson.Unmarshal(it.Def, v)
			v.AppId, v.Id = app, it.ID
			out.Views = append(out.Views, v)
		default:
			b := &nanashiv1.BoardDef{}
			err = protojson.Unmarshal(it.Def, b)
			b.AppId, b.Id = app, it.ID
			out.Boards = append(out.Boards, b)
		}
		if err != nil {
			return nil, dbError(err)
		}
	}
	return connect.NewResponse(out), nil
}

func (s *PlanServer) CreateList(ctx context.Context, req *connect.Request[nanashiv1.CreateListRequest]) (*ack, error) {
	m := req.Msg
	if m.Kind != nanashiv1.ListKind_LIST_KIND_DIMENSION && m.Kind != nanashiv1.ListKind_LIST_KIND_TRANSACTION {
		return nil, invalid(errors.New("作れるリストは DIMENSION か TRANSACTION"))
	}
	if strings.TrimSpace(m.Name) == "" {
		return nil, invalid(errors.New("リストの名前が空"))
	}
	members := m.Members
	if members == nil {
		members = []string{}
	}
	return s.writeWithList(ctx, m.AppId, m.Kind, []op{newOp("add_dimension", m.Name, members)}, m.Name)
}

// writeWithList writes the operations and then records the kind of the new lists.
func (s *PlanServer) writeWithList(ctx context.Context, app string, kind nanashiv1.ListKind, ops []op, lists ...string) (*ack, error) {
	if err := s.Engines.write(ctx, app, callerOf(ctx).user, ops); err != nil {
		return nil, err
	}
	for _, list := range lists {
		if _, err := s.Pool.Exec(ctx, `insert into app_list (app_id, name, kind) values ($1, $2, $3)
			on conflict (app_id, name) do update set kind = excluded.kind`, app, list, kind); err != nil {
			return nil, dbError(err)
		}
	}
	return ok()
}

func (s *PlanServer) AddProperty(ctx context.Context, req *connect.Request[nanashiv1.AddPropertyRequest]) (*ack, error) {
	app, list, p := req.Msg.AppId, req.Msg.List, req.Msg.Property
	if p == nil || strings.TrimSpace(p.Name) == "" {
		return nil, invalid(errors.New("プロパティの名前が空"))
	}
	defer s.lock(app)()
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	dim, found := em.dim(list)
	if !found {
		return nil, invalid(fmt.Errorf("リスト %s がない", list))
	}
	meta, err := s.meta(ctx, app)
	if err != nil {
		return nil, err
	}
	// The engine knows only the DIMENSION properties. The api tables have the other types.
	exists := slices.ContainsFunc(dim.Props, func(x engineProp) bool { return x.Name == p.Name }) ||
		slices.ContainsFunc(meta.Props, func(x propRow) bool { return x.List == list && x.Name == p.Name })
	if exists {
		return nil, connect.NewError(connect.CodeAlreadyExists, fmt.Errorf("%s にプロパティ %s はすでにある", list, p.Name))
	}
	var ops []op
	switch kind, isMetric := propKind[p.Type]; {
	case isMetric:
		ops = []op{newOp("add_input", propMetric(list, p.Name), []string{list}, []any{}).with(map[string]any{"kind": kind})}
	case p.Type == nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION:
		ops = []op{newOp("add_property", list, p.Name, p.Target, map[string]string{})}
	case p.Type != nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
		return nil, invalid(errors.New("プロパティの型を指定する"))
	}
	if p.Type != nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION {
		// The property Metric must not replace a Metric of the user. add_input replaces the cells.
		if _, err := em.metric(propMetric(list, p.Name)); err == nil {
			return nil, connect.NewError(connect.CodeAlreadyExists, fmt.Errorf("Metric %s があるので、プロパティ %s を作れない", propMetric(list, p.Name), p.Name))
		}
		// Record first: the primary key refuses a second property with the same name.
		tag, err := s.Pool.Exec(ctx, `insert into app_property (app_id, list, name, type) values ($1, $2, $3, $4)
			on conflict do nothing`, app, list, p.Name, p.Type)
		if err != nil {
			return nil, dbError(err)
		}
		if tag.RowsAffected() == 0 {
			return nil, connect.NewError(connect.CodeAlreadyExists, fmt.Errorf("%s にプロパティ %s はすでにある", list, p.Name))
		}
	}
	if err := s.Engines.write(ctx, app, callerOf(ctx).user, ops); err != nil {
		s.Pool.Exec(ctx, "delete from app_property where app_id = $1 and list = $2 and name = $3", app, list, p.Name)
		return nil, err
	}
	return ok()
}

func (s *PlanServer) EditMembers(ctx context.Context, req *connect.Request[nanashiv1.EditMembersRequest]) (*ack, error) {
	return s.editMembers(ctx, req.Msg.AppId, req.Msg.List, func(engineModel, appMeta) ([]*nanashiv1.MemberEdit, error) {
		return req.Msg.Edits, nil
	})
}

// editMembers makes the edits with editsOf inside the lock, so that the edits see the current members
// (for example, the next row number of a transaction list).
func (s *PlanServer) editMembers(ctx context.Context, app, list string, editsOf func(engineModel, appMeta) ([]*nanashiv1.MemberEdit, error)) (*ack, error) {
	defer s.lock(app)()
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	meta, err := s.meta(ctx, app)
	if err != nil {
		return nil, err
	}
	edits, err := editsOf(em, meta)
	if err != nil {
		return nil, invalid(err)
	}
	ops, text, err := editOps(list, em, meta, edits)
	if err != nil {
		return nil, invalid(err)
	}
	if err := s.Engines.write(ctx, app, callerOf(ctx).user, ops); err != nil {
		return nil, err
	}
	for prop, values := range text {
		if _, err := s.Pool.Exec(ctx, "update app_property set text_values = $4 where app_id = $1 and list = $2 and name = $3",
			app, list, prop, textJSON(values)); err != nil {
			return nil, dbError(err)
		}
	}
	// Access rules and view filters refer to members by name. Give them the new name, or remove the name of a
	// removed member: if a later edit adds the name again, an old rule must not give access to it.
	for _, e := range edits {
		var old string
		var name *string // nil removes the name.
		if r := e.GetRename(); r != nil {
			n := strings.TrimSpace(r.NewName) // editOps gives the engine the same name.
			old, name = r.Name, &n
		} else if r := e.GetRemove(); r != nil {
			old = r.Name
		} else {
			continue
		}
		if _, err := s.Pool.Exec(ctx, `update app_access_rule set members = coalesce((select jsonb_agg(case when m = to_jsonb($3::text) then to_jsonb($4::text) else m end)
			from jsonb_array_elements(members) m where m <> to_jsonb($3::text) or $4::text is not null), '[]')
			where app_id = $1 and list = $2 and members ? $3`, app, list, old, name); err != nil {
			return nil, dbError(err)
		}
		if _, err := s.Pool.Exec(ctx, `update app_item set def = jsonb_set(def, array['filters', $2, 'names'], coalesce(
			(select jsonb_agg(case when n = to_jsonb($3::text) then to_jsonb($4::text) else n end) from jsonb_array_elements(def->'filters'->$2->'names') n
			where n <> to_jsonb($3::text) or $4::text is not null), '[]'))
			where app_id = $1 and def->'filters'->$2->'names' ? $3`, app, list, old, name); err != nil {
			return nil, dbError(err)
		}
		// Cell comments of a removed member stay as they are. The comments list still shows them.
		if _, err := s.Pool.Exec(ctx, `update app_comment set cell = jsonb_set(cell, array[$2], to_jsonb($4::text))
			where app_id = $1 and cell->>$2 = $3 and $4::text is not null`, app, list, old, name); err != nil {
			return nil, dbError(err)
		}
	}
	return ok()
}

func (s *PlanServer) CreateCalendar(ctx context.Context, req *connect.Request[nanashiv1.CreateCalendarRequest]) (*ack, error) {
	ops, err := calendarOps(int(req.Msg.StartYear), int(req.Msg.Years))
	if err != nil {
		return nil, invalid(err)
	}
	return s.writeWithList(ctx, req.Msg.AppId, nanashiv1.ListKind_LIST_KIND_CALENDAR, ops, "Year", "Quarter", "Month")
}

const scenarioList = "Scenario"

func (s *PlanServer) CreateScenario(ctx context.Context, req *connect.Request[nanashiv1.CreateScenarioRequest]) (*ack, error) {
	app, name, from := req.Msg.AppId, strings.TrimSpace(req.Msg.Name), req.Msg.CopyFrom
	if name == "" {
		return nil, invalid(errors.New("シナリオの名前が空"))
	}
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	if _, found := em.dim(scenarioList); !found {
		if from != "" {
			return nil, invalid(fmt.Errorf("シナリオ %s がない", from))
		}
		return s.writeWithList(ctx, app, nanashiv1.ListKind_LIST_KIND_SCENARIO,
			[]op{newOp("add_dimension", scenarioList, []string{name})}, scenarioList)
	}
	ops := []op{newOp("add_member", scenarioList, name)}
	if from != "" {
		for _, m := range em.Metrics {
			if m.Formula != "" || !slices.Contains(m.Dims, scenarioList) {
				continue
			}
			cube, err := s.Engines.read(ctx, app, engineRead{Metric: m.Name, Path: "slice", Query: map[string][]string{scenarioList: {from}}})
			if err != nil {
				return nil, err
			}
			ops = append(ops, copyCellOps(m.Name, scenarioList, name, cube)...)
		}
	}
	if err := s.Engines.write(ctx, app, callerOf(ctx).user, ops); err != nil {
		return nil, err
	}
	return ok()
}

func (s *PlanServer) SaveMetric(ctx context.Context, req *connect.Request[nanashiv1.SaveMetricRequest]) (*ack, error) {
	app, m := req.Msg.AppId, req.Msg.Metric
	if m == nil || strings.TrimSpace(m.Name) == "" {
		return nil, invalid(errors.New("Metric の名前が空"))
	}
	defer s.lock(app)()
	if err := s.refusePropertyMetric(ctx, app, m.Name); err != nil {
		return nil, err
	}
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	kind, err := engineKind(em, m)
	if err != nil {
		return nil, invalid(err)
	}
	ops, err := metricOp(em, m, kind, req.Msg.Replace)
	if err != nil {
		return nil, connectError(err)
	}
	if len(ops) == 0 {
		return ok()
	}
	if err := s.Engines.write(ctx, app, callerOf(ctx).user, ops); err != nil {
		return nil, err
	}
	return ok()
}

// refusePropertyMetric refuses a change to a Metric with the name of a property Metric. GetModel needs that Metric.
func (s *PlanServer) refusePropertyMetric(ctx context.Context, app string, names ...string) error {
	meta, err := s.meta(ctx, app)
	if err != nil {
		return err
	}
	for _, n := range names {
		if meta.holdsProperty(n) {
			return connect.NewError(connect.CodeFailedPrecondition, fmt.Errorf("%s はプロパティの値を持つ Metric なので、変更できない", n))
		}
	}
	return nil
}

func (s *PlanServer) RenameMetric(ctx context.Context, req *connect.Request[nanashiv1.RenameMetricRequest]) (*ack, error) {
	if err := s.refusePropertyMetric(ctx, req.Msg.AppId, req.Msg.Name, req.Msg.NewName); err != nil {
		return nil, err
	}
	if err := s.Engines.write(ctx, req.Msg.AppId, callerOf(ctx).user, []op{newOp("rename_metric", req.Msg.Name, req.Msg.NewName)}); err != nil {
		return nil, err
	}
	// Tables, views and comments refer to Metrics by name, so give them the new name too.
	if _, err := s.Pool.Exec(ctx, `update app_item set def = jsonb_set(def, '{metrics}',
		(select jsonb_agg(case when m = to_jsonb($2::text) then to_jsonb($3::text) else m end) from jsonb_array_elements(def->'metrics') m))
		where app_id = $1 and def->'metrics' ? $2`, req.Msg.AppId, req.Msg.Name, req.Msg.NewName); err != nil {
		return nil, dbError(err)
	}
	if _, err := s.Pool.Exec(ctx, "update app_comment set metric = $3 where app_id = $1 and metric = $2",
		req.Msg.AppId, req.Msg.Name, req.Msg.NewName); err != nil {
		return nil, dbError(err)
	}
	return ok()
}

func (s *PlanServer) DeleteMetric(ctx context.Context, req *connect.Request[nanashiv1.DeleteMetricRequest]) (*ack, error) {
	if err := s.refusePropertyMetric(ctx, req.Msg.AppId, req.Msg.Name); err != nil {
		return nil, err
	}
	if err := s.Engines.write(ctx, req.Msg.AppId, callerOf(ctx).user, []op{newOp("remove_metric", req.Msg.Name)}); err != nil {
		return nil, err
	}
	// A table or a view with a deleted Metric cannot query, so remove the name from them.
	if _, err := s.Pool.Exec(ctx, `update app_item set def = jsonb_set(def, '{metrics}',
		coalesce((select jsonb_agg(m) from jsonb_array_elements(def->'metrics') m where m <> to_jsonb($2::text)), '[]'))
		where app_id = $1 and def->'metrics' ? $2`, req.Msg.AppId, req.Msg.Name); err != nil {
		return nil, dbError(err)
	}
	return ok()
}

func (s *PlanServer) Query(ctx context.Context, req *connect.Request[nanashiv1.QueryRequest]) (*connect.Response[nanashiv1.QueryResponse], error) {
	app := req.Msg.AppId
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	l := callerOf(ctx).limitsIn(em)
	reads, err := queryReads(req.Msg, em, l)
	if err != nil {
		return nil, invalid(err)
	}
	dims := slices.Concat(req.Msg.Rows, req.Msg.Columns)
	out := &nanashiv1.QueryResponse{Dimensions: dims}
	for _, r := range reads {
		cube, err := s.Engines.read(ctx, app, r)
		if err != nil {
			return nil, err
		}
		m, _ := em.metric(r.Metric) // queryReads found it.
		out.Cells = append(out.Cells, queryCells(m, dims, cube, l)...)
	}
	return connect.NewResponse(out), nil
}

func (s *PlanServer) WriteCells(ctx context.Context, req *connect.Request[nanashiv1.WriteCellsRequest]) (*ack, error) {
	em, _, err := s.Engines.model(ctx, req.Msg.AppId)
	if err != nil {
		return nil, err
	}
	c := callerOf(ctx)
	ops, err := writeOps(req.Msg.Writes, em, c.limitsIn(em))
	if err != nil {
		return nil, connectError(err)
	}
	if err := s.Engines.write(ctx, req.Msg.AppId, c.user, ops); err != nil {
		return nil, err
	}
	return ok()
}

func (s *PlanServer) Import(ctx context.Context, req *connect.Request[nanashiv1.ImportRequest]) (*connect.Response[nanashiv1.ImportResponse], error) {
	app, c := req.Msg.AppId, callerOf(ctx)
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	var rows int
	switch t := req.Msg.Target.(type) {
	case *nanashiv1.ImportRequest_List:
		if _, ruled := c.limitsIn(em)[t.List.List]; ruled {
			return nil, connect.NewError(connect.CodePermissionDenied, fmt.Errorf("%s には権限の制限があるので読み込めない", t.List.List))
		}
		if _, err := s.editMembers(ctx, app, t.List.List, func(em engineModel, meta appMeta) ([]*nanashiv1.MemberEdit, error) {
			edits, n, err := importListEdits(req.Msg.Csv, t.List, em, meta.Kinds[t.List.List])
			rows = n
			return edits, err
		}); err != nil {
			return nil, err
		}
	case *nanashiv1.ImportRequest_Metric:
		ops, n, err := importMetricOps(req.Msg.Csv, t.Metric, em, c.limitsIn(em))
		if err != nil {
			return nil, connectError(err)
		}
		if err := s.Engines.write(ctx, app, c.user, ops); err != nil {
			return nil, err
		}
		rows = n
	default:
		return nil, invalid(errors.New("読み込み先を指定する"))
	}
	return connect.NewResponse(&nanashiv1.ImportResponse{Rows: int32(rows)}), nil
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

func (s *PlanServer) ListComments(ctx context.Context, req *connect.Request[nanashiv1.ListCommentsRequest]) (*connect.Response[nanashiv1.ListCommentsResponse], error) {
	rows, _ := s.Pool.Query(ctx, `select id, metric, cell, user_name, body, (extract(epoch from created_at) * 1000)::bigint from app_comment
		where app_id = $1 and ($2 = '' or metric = $2) order by id`, req.Msg.AppId, req.Msg.Metric)
	comments, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Comment, error) {
		c := &nanashiv1.Comment{AppId: req.Msg.AppId}
		var id int64
		err := row.Scan(&id, &c.Metric, &c.Cell, &c.User, &c.Body, &c.CreatedAt)
		c.Id = strconv.FormatInt(id, 10)
		return c, err
	})
	if err != nil {
		return nil, dbError(err)
	}
	if c := callerOf(ctx); len(c.rules) > 0 {
		em, _, err := s.Engines.model(ctx, req.Msg.AppId)
		if err != nil {
			return nil, err
		}
		comments = visibleComments(comments, c.limitsIn(em))
	}
	return connect.NewResponse(&nanashiv1.ListCommentsResponse{Comments: comments}), nil
}

func (s *PlanServer) AddComment(ctx context.Context, req *connect.Request[nanashiv1.Comment]) (*connect.Response[nanashiv1.Comment], error) {
	c := req.Msg
	if strings.TrimSpace(c.Body) == "" || c.Metric == "" {
		return nil, invalid(errors.New("コメントのメトリックと本文が要る"))
	}
	c.User = callerOf(ctx).user
	var id int64
	if err := s.Pool.QueryRow(ctx, `insert into app_comment (app_id, metric, cell, user_name, body) values ($1, $2, $3, $4, $5)
		returning id, (extract(epoch from created_at) * 1000)::bigint`, c.AppId, c.Metric, textJSON(c.Cell), c.User, c.Body).Scan(&id, &c.CreatedAt); err != nil {
		return nil, dbError(err)
	}
	c.Id = strconv.FormatInt(id, 10)
	return connect.NewResponse(c), nil
}

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
	em, raw, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	data := snapshotData{Engine: raw, Inputs: map[string]engineCube{}, Overrides: map[string]engineCube{}}
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

func (s *PlanServer) rules(ctx context.Context, app string) ([]*nanashiv1.AccessRule, error) {
	rows, _ := s.Pool.Query(ctx, "select id, role, list, members, write from app_access_rule where app_id = $1 order by id", app)
	rules, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.AccessRule, error) {
		r := &nanashiv1.AccessRule{AppId: app}
		return r, row.Scan(&r.Id, &r.Role, &r.List, &r.Members, &r.Write)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return rules, nil
}

func (s *PlanServer) GetAccess(ctx context.Context, req *connect.Request[nanashiv1.GetAccessRequest]) (*connect.Response[nanashiv1.Access], error) {
	app := req.Msg.AppId
	rows, _ := s.Pool.Query(ctx, "select user_name, role from app_member where app_id = $1 order by user_name", app)
	members, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.AppMember, error) {
		m := &nanashiv1.AppMember{AppId: app}
		return m, row.Scan(&m.User, &m.Role)
	})
	if err != nil {
		return nil, dbError(err)
	}
	rules, err := s.rules(ctx, app)
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&nanashiv1.Access{Members: members, Rules: rules}), nil
}

func (s *PlanServer) SetMemberRole(ctx context.Context, req *connect.Request[nanashiv1.AppMember]) (*ack, error) {
	m := req.Msg
	if strings.TrimSpace(m.User) == "" {
		return nil, invalid(errors.New("利用者が空"))
	}
	err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		// Lock the members of the application, so that two ADMINs cannot remove each other at the same time.
		if _, err := tx.Exec(ctx, "select 1 from app_member where app_id = $1 for update", m.AppId); err != nil {
			return err
		}
		var err error
		if m.Role == nanashiv1.Role_ROLE_UNSPECIFIED {
			_, err = tx.Exec(ctx, "delete from app_member where app_id = $1 and user_name = $2", m.AppId, m.User)
		} else {
			_, err = tx.Exec(ctx, `insert into app_member (app_id, user_name, role) values ($1, $2, $3)
				on conflict (app_id, user_name) do update set role = excluded.role`, m.AppId, m.User, m.Role)
		}
		if err != nil {
			return err
		}
		var admins int
		if err := tx.QueryRow(ctx, "select count(*) from app_member where app_id = $1 and role = $2", m.AppId, admin).Scan(&admins); err != nil {
			return err
		}
		if admins == 0 {
			return connect.NewError(connect.CodeFailedPrecondition, errors.New("最後の ADMIN は外せない"))
		}
		return nil
	})
	if cerr := new(connect.Error); errors.As(err, &cerr) {
		return nil, err
	}
	if err != nil {
		return nil, dbError(err)
	}
	return ok()
}

func (s *PlanServer) SaveAccessRule(ctx context.Context, req *connect.Request[nanashiv1.AccessRule]) (*connect.Response[nanashiv1.AccessRule], error) {
	r := req.Msg
	if r.Role != viewer && r.Role != contributor {
		return nil, invalid(errors.New("ルールは VIEWER か CONTRIBUTOR に付ける（MODELER と ADMIN はルールを無視する）"))
	}
	em, _, err := s.Engines.model(ctx, r.AppId)
	if err != nil {
		return nil, err
	}
	d, found := em.dim(r.List)
	if !found {
		return nil, invalid(fmt.Errorf("リスト %s がない", r.List))
	}
	for _, x := range r.Members {
		if !slices.Contains(d.Members, x) {
			return nil, invalid(fmt.Errorf("%s に %q がない", r.List, x))
		}
	}
	if r.Id == "" {
		r.Id = newID()
	}
	members := r.Members
	if members == nil {
		members = []string{}
	}
	if _, err := s.Pool.Exec(ctx, `insert into app_access_rule (app_id, id, role, list, members, write) values ($1, $2, $3, $4, $5, $6)
		on conflict (app_id, id) do update set role = excluded.role, list = excluded.list, members = excluded.members, write = excluded.write`,
		r.AppId, r.Id, r.Role, r.List, members, r.Write); err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(r), nil
}

func (s *PlanServer) DeleteAccessRule(ctx context.Context, req *connect.Request[nanashiv1.DeleteAccessRuleRequest]) (*ack, error) {
	if _, err := s.Pool.Exec(ctx, "delete from app_access_rule where app_id = $1 and id = $2", req.Msg.AppId, req.Msg.Id); err != nil {
		return nil, dbError(err)
	}
	return ok()
}
