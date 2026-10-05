// Package api is the application server. It gives the Connect API (nanashi.v1.PlanService) to the frontend.
package api

import (
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"path"

	"connectrpc.com/connect"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

//go:embed schema.sql
var schema string

// PlanServer implements PlanService. It keeps its own data in PostgreSQL and the model in the engines.
// It has no lock. The engine (one writer) orders the engine writes. The PostgreSQL constraints order the api rows.
// The outbox (app_operation, see change) connects the two.
type PlanServer struct {
	Pool    *pgxpool.Pool
	Engines *Engines
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
	"CreateCalendar": {modeler, true}, "CreateScenario": {modeler, true}, "CreateMetric": {modeler, true},
	"UpdateMetric": {modeler, true}, "RenameMetric": {modeler, true}, "DeleteMetric": {modeler, true},
	"CreateTable": {modeler, true}, "UpdateTable": {modeler, true}, "CreateView": {modeler, true},
	"UpdateView": {modeler, true}, "CreateBoard": {modeler, true}, "UpdateBoard": {modeler, true},
	"DeleteItem": {modeler, true}, "GetAccess": {min: admin}, "SetMemberRole": {admin, true},
	"CreateAccessRule": {admin, true}, "UpdateAccessRule": {admin, true}, "DeleteAccessRule": {admin, true},
}

// caller is the user of a request and the rights of the user in the application of the request.
type caller struct {
	user   string
	method string // The RPC name, for example "CreateList".
	opID   string // The client_op_id of a request that changes data, in canonical form. Empty for a read.
	role   role
	rules  []*nanashiv1.AccessRule // The access rules of the application. Empty when role >= MODELER.
}

func (c caller) limitsIn(em engineModel) limits { return accessLimits(c.role, c.rules, em).through(em) }

// message is a request message.
type message = proto.Message

type callerKey struct{}

func callerOf(ctx context.Context) caller { return ctx.Value(callerKey{}).(caller) }

// constraintMessages gives the message of a unique violation by the constraint name.
var constraintMessages = map[string]string{
	"app_application_pkey": "同じ id のアプリケーションがすでにある",
	"app_list_pkey":        "同じ id のリストがすでにある",
	"app_property_pkey":    "同じ id のプロパティがすでにある",
	"app_property_name":    "同じ名前のプロパティがすでにある",
	"app_item_pkey":        "同じ id がすでにある",
	"app_metric_pkey":      "同じ id のメトリックがすでにある",
	"app_comment_pkey":     "同じ id のコメントがすでにある",
	"app_access_rule_pkey": "同じ id のルールがすでにある",
	"app_snapshot_pkey":    "同じ id のスナップショットがすでにある",
}

// dbError hides the database error from the client. It can contain table names and addresses.
// A *connect.Error passes through. A tagged error gets its code.
func dbError(err error) error {
	if cerr := new(connect.Error); errors.As(err, &cerr) {
		return err
	}
	for sentinel := range errorCodes {
		if errors.Is(err, sentinel) {
			return connectError(err)
		}
	}
	// Two api processes can pass the same check for a free id or name. Then the second insert breaks a unique key.
	if pe := new(pgconn.PgError); errors.As(err, &pe) && pe.Code == "23505" /* unique_violation */ {
		msg, ok := constraintMessages[pe.ConstraintName]
		if !ok {
			msg = "同じ id がすでにある"
		}
		return connect.NewError(connect.CodeAlreadyExists, errors.New(msg))
	}
	log.Printf("database: %v", err)
	return connect.NewError(connect.CodeUnavailable, errors.New("データベースを読み書きできない"))
}

func isDeadlock(err error) bool {
	pe := new(pgconn.PgError)
	return errors.As(err, &pe) && pe.Code == "40P01"
}

func invalid(err error) error { return connect.NewError(connect.CodeInvalidArgument, err) }

// parseID gives the canonical form (lower case, with hyphens) of a UUID from a request.
// If s is not a UUID, it gives an InvalidArgument error.
func parseID(field, s string) (string, error) {
	u, err := uuid.Parse(s)
	if err != nil || len(s) != 36 {
		return "", invalid(fmt.Errorf("%s が UUID ではない: %q", field, s))
	}
	return u.String(), nil
}

// newID makes a UUIDv7 for an object that the api makes for the user.
func newID() string { return uuid.Must(uuid.NewV7()).String() }

// Request fields that hold ids: every string field with one of these names, the keys of these map fields, and
// the values of these map fields. canonicalize changes them to the canonical form.
var (
	idFields = map[string]bool{"id": true, "app_id": true, "client_op_id": true, "snapshot_id": true, "metric": true,
		"list": true, "target": true, "member_list": true, "dimensions": true, "rows": true, "columns": true,
		"metrics": true, "members": true, "ids": true, "page_selectors": true, "copy_from": true, "view_id": true, "member": true}
	idKeys   = map[string]bool{"filters": true, "coords": true, "cell": true, "properties": true, "property_columns": true, "dimension_columns": true}
	idValues = map[string]bool{"coords": true, "cell": true}
)

// canonicalize changes each id in a request to the canonical form. An id that is not a UUID gives InvalidArgument.
// An empty string field is not set, so an optional reference stays empty.
func canonicalize(m protoreflect.Message) error {
	var fail error
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		name := string(fd.Name())
		var err error
		switch {
		case fd.IsMap():
			err = canonicalizeMap(name, fd, v.Map())
		case fd.IsList() && fd.Kind() == protoreflect.StringKind && idFields[name]:
			l := v.List()
			for i := 0; i < l.Len() && err == nil; i++ {
				var s string
				if s, err = parseID(name, l.Get(i).String()); err == nil {
					l.Set(i, protoreflect.ValueOfString(s))
				}
			}
		case fd.IsList() && fd.Kind() == protoreflect.MessageKind:
			l := v.List()
			for i := 0; i < l.Len() && err == nil; i++ {
				err = canonicalize(l.Get(i).Message())
			}
		case fd.Kind() == protoreflect.MessageKind:
			err = canonicalize(v.Message())
		case fd.Kind() == protoreflect.StringKind && idFields[name]:
			var s string
			if s, err = parseID(name, v.String()); err == nil {
				m.Set(fd, protoreflect.ValueOfString(s))
			}
		}
		fail = err
		return err == nil
	})
	return fail
}

func canonicalizeMap(name string, fd protoreflect.FieldDescriptor, mv protoreflect.Map) error {
	keys, values := idKeys[name], idValues[name]
	messages := fd.MapValue().Kind() == protoreflect.MessageKind
	if !keys && !values && !messages {
		return nil
	}
	type entry struct {
		k protoreflect.MapKey
		v protoreflect.Value
	}
	var entries []entry
	mv.Range(func(k protoreflect.MapKey, v protoreflect.Value) bool {
		entries = append(entries, entry{k, v})
		return true
	})
	for _, e := range entries {
		k, v := e.k, e.v
		if keys {
			s, err := parseID(name, k.String())
			if err != nil {
				return err
			}
			k = protoreflect.ValueOfString(s).MapKey()
		}
		if values {
			s, err := parseID(name, v.String())
			if err != nil {
				return err
			}
			v = protoreflect.ValueOfString(s)
		}
		if messages {
			if err := canonicalize(v.Message()); err != nil {
				return err
			}
		}
		mv.Clear(e.k)
		mv.Set(k, v)
	}
	return nil
}

// requestHash identifies the content of a request without its client_op_id. A resend with the same client_op_id
// and another hash is an error. protojson with sorted keys is stable across builds.
func requestHash(m proto.Message) string {
	b, _ := protojson.Marshal(m)
	var v map[string]any
	json.Unmarshal(b, &v)
	delete(v, "clientOpId")
	b, _ = json.Marshal(v)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

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
	errNotFound:     connect.CodeNotFound,
	errPrecondition: connect.CodeFailedPrecondition,
}

func ok() (*ack, error) { return connect.NewResponse(&nanashiv1.Ack{}), nil }

func textJSON(m map[string]string) string {
	if m == nil {
		m = map[string]string{}
	}
	b, _ := json.Marshal(m)
	return string(b)
}

// stmt is one statement of the api tables in the first transaction of a change. If zero is not nil and the
// statement changes no row, the transaction rolls back with zero (an errExists or errNotFound error).
type stmt struct {
	sql  string
	args []any
	zero error
}

// madeRow is a row that the first transaction makes or changes. A refusal of the engine deletes it, or for
// app_metric puts Old back (compensation).
type madeRow struct {
	Table  string `json:"table"`
	ID     string `json:"id"`
	ListID string `json:"list_id,omitempty"`
	// app_metric only. New is the values that the change wrote. Old is the values before the change (nil: no row).
	// The compensation changes the row only if it still has New, so it does not undo a later change.
	Old *metricRow `json:"old,omitempty"`
	New *metricRow `json:"new,omitempty"`
}

// plan is one change to an application: the engine operations, the statements for the api tables, the rows
// that the statements make, and the result for the client.
type plan struct {
	ops    []op
	stmts  []stmt
	made   []madeRow
	expect bool  // Send the version of the model that the plan read: the engine refuses a conflicting write.
	seq    int64 // The version of the model that the plan read.
	result any   // The stored result of the operation. nil gives {}.
}

// opRow is a row of app_operation.
type opRow struct {
	opID, user, method, hash, status string
	ops                              []op
	seq                              int64
	expect                           bool
	made                             []madeRow
	result                           json.RawMessage
	err                              json.RawMessage
}

type storedError struct {
	Code    connect.Code `json:"code"`
	Message string       `json:"message"`
}

// error gives the stored error of a failed row.
func (r opRow) error() error {
	var e storedError
	json.Unmarshal(r.err, &e)
	return connect.NewError(e.Code, errors.New(e.Message))
}

// same tells if a resend has the same user, method and content as the row.
func (r opRow) same(c caller, hash string) error {
	if r.user != c.user || r.method != c.method || r.hash != hash {
		return invalid(errors.New("同じ client_op_id の別の要求がすでにある"))
	}
	return nil
}

// compensation gives the statements that delete the rows of a refused change.
func compensation(app string, made []madeRow) []stmt {
	var out []stmt
	for _, m := range made {
		switch m.Table {
		case "app_application":
			out = append(out, stmt{sql: "delete from app_application where id = $1", args: []any{app}})
		case "app_list":
			out = append(out, stmt{sql: "delete from app_list where app_id = $1 and id = $2", args: []any{app, m.ID}})
		case "app_property":
			out = append(out, stmt{sql: "delete from app_property where app_id = $1 and list_id = $2 and id = $3", args: []any{app, m.ListID, m.ID}})
		case "app_metric":
			key := "where app_id = $1 and metric_id = $2 and description = $3 and folder = $4"
			args := []any{app, m.ID, m.New.Description, m.New.Folder}
			if m.Old == nil {
				out = append(out, stmt{sql: "delete from app_metric " + key, args: args})
			} else {
				out = append(out, stmt{sql: "update app_metric set description = $5, folder = $6 " + key, args: append(args, m.Old.Description, m.Old.Folder)})
			}
		}
	}
	return out
}

// The tags give an error its Connect code (connectError). An error without a tag is an input error.
var (
	errDenied       = errors.New("permission denied")
	errExists       = errors.New("already exists")
	errNotFound     = errors.New("not found")
	errPrecondition = errors.New("failed precondition")
)

// tagged is an error with a tag. Error() gives only the message of err, so the user sees the Japanese text.
type tagged struct{ err, tag error }

func (t tagged) Error() string   { return t.err.Error() }
func (t tagged) Unwrap() []error { return []error{t.err, t.tag} }

func tag(t error, format string, a ...any) error { return tagged{fmt.Errorf(format, a...), t} }

// The actions follow.

// Migrate makes the api tables (prefix app_) if they are missing.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx, schema)
	return err
}

// Interceptor identifies the user, changes the ids to the canonical form, checks the role for the RPC, and
// records the audit trail.
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
			msg := req.Any().(proto.Message)
			if err := canonicalize(msg.ProtoReflect()); err != nil {
				return nil, err
			}
			c := caller{user: user, method: method}
			appID := ""
			if a, ok := req.Any().(interface{ GetAppId() string }); ok {
				appID = a.GetAppId()
			}
			if rule.min != nanashiv1.Role_ROLE_UNSPECIFIED {
				if appID == "" {
					return nil, invalid(errors.New("app_id が要る"))
				}
				var err error
				if c.role, c.rules, err = s.rights(ctx, appID, user); err != nil {
					return nil, err
				}
				if c.role < rule.min {
					return nil, connect.NewError(connect.CodePermissionDenied, errors.New("この操作をする権限がない"))
				}
			}
			// Each request that changes data has a client_op_id. canonicalize checked its form.
			if r, ok := req.Any().(interface{ GetClientOpId() string }); ok {
				if c.opID = r.GetClientOpId(); c.opID == "" {
					return nil, invalid(errors.New("client_op_id が要る"))
				}
			}
			res, err := next(context.WithValue(ctx, callerKey{}, c), req)
			if err == nil && rule.audit {
				if a, ok := res.Any().(*nanashiv1.Application); ok {
					appID = a.Id
				}
				detail, _ := protojson.Marshal(msg)
				if _, err := s.Pool.Exec(ctx, "insert into app_audit (app_id, user_name, action, detail) values ($1, $2, $3, $4)",
					appID, user, method, string(detail)); err != nil {
					log.Printf("audit %s %s: %v", appID, method, err)
				}
			}
			return res, err
		}
	}
}

// doneApps is the condition of an application whose creation is done. The api shows no other application.
const doneApps = `exists (select 1 from app_operation o where o.app_id = a.id and o.method = 'CreateApplication' and o.status = 'done')`

func (s *PlanServer) ListApplications(ctx context.Context, _ *connect.Request[nanashiv1.ListApplicationsRequest]) (*connect.Response[nanashiv1.ListApplicationsResponse], error) {
	rows, _ := s.Pool.Query(ctx, `select a.id, a.name, m.role from app_application a
		join app_member m on m.app_id = a.id where m.user_name = $1 and `+doneApps+` order by a.created_at, a.id`, callerOf(ctx).user)
	apps, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Application, error) {
		a := &nanashiv1.Application{}
		return a, row.Scan(&a.Id, &a.Name, &a.Role)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.ListApplicationsResponse{Applications: apps}), nil
}

// lookupOp reads the row of a client_op_id.
func (s *PlanServer) lookupOp(ctx context.Context, app, opID string) (opRow, bool, error) {
	r := opRow{opID: opID}
	err := s.Pool.QueryRow(ctx, `select user_name, method, request_hash, status, coalesce(ops, '[]'), coalesce(seq, 0), expect,
		coalesce(made, '[]'), coalesce(result, '{}'), coalesce(error, '{}') from app_operation where app_id = $1 and client_op_id = $2`, app, opID).
		Scan(&r.user, &r.method, &r.hash, &r.status, &r.ops, &r.seq, &r.expect, &r.made, &r.result, &r.err)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, false, nil
	}
	if err != nil {
		return r, false, dbError(err)
	}
	return r, true, nil
}

// change applies a change that the engine and the api tables both take (the outbox, issue #6).
//  1. If app_operation has the client_op_id, the stored result or error comes back. A pending row is settled.
//  2. The other pending rows of the application are settled in their order.
//  3. planOf reads the model and the api data and makes the plan. The ids that the api makes come from here.
//  4. The first transaction inserts the pending row and runs the api statements. A constraint violation rolls back
//     before the engine sees anything.
//  5. settle sends the operations to the engine and flips the row to done or failed in the second transaction.
func (s *PlanServer) change(ctx context.Context, app string, req proto.Message, planOf func(engineModel, appMeta) (plan, error)) (json.RawMessage, error) {
	return s.outbox(ctx, app, req, func() (plan, error) {
		em, _, err := s.Engines.model(ctx, app)
		if err != nil {
			return plan{}, err
		}
		meta, err := s.meta(ctx, app)
		if err != nil {
			return plan{}, err
		}
		p, err := planOf(em, meta)
		if err != nil {
			return plan{}, connectError(err)
		}
		p.seq = em.Seq
		return p, nil
	})
}

func (s *PlanServer) outbox(ctx context.Context, app string, req proto.Message, planOf func() (plan, error)) (json.RawMessage, error) {
	c := callerOf(ctx)
	hash := requestHash(req)
	conflicts := 0
	// again tells if a conflict of the engine (409 conflict, not recorded) lets the plan run again.
	again := func(err error) bool {
		conflicts++
		return errors.Is(err, errConflict) && conflicts <= 3
	}
	for {
		row, found, err := s.lookupOp(ctx, app, c.opID)
		if err != nil {
			return nil, err
		}
		if found {
			if err := row.same(c, hash); err != nil {
				return nil, err
			}
			switch row.status {
			case "done":
				return row.result, nil
			case "failed":
				return nil, row.error()
			}
			result, err := s.settle(ctx, app, row)
			if again(err) {
				continue
			}
			return result, err
		}
		if err := s.settlePending(ctx, app); err != nil {
			return nil, err
		}
		p, err := planOf()
		if err != nil {
			return nil, err
		}
		seq := p.seq
		result, err := json.Marshal(p.result)
		if err != nil || p.result == nil {
			result = []byte("{}")
		}
		row = opRow{opID: c.opID, user: c.user, method: c.method, hash: hash, status: "pending", ops: p.ops, seq: seq, expect: p.expect, made: p.made, result: result}
		inserted := false
		err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
			tag, err := tx.Exec(ctx, `insert into app_operation (app_id, client_op_id, user_name, method, request_hash, ops, seq, expect, made, result, status)
				values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending') on conflict do nothing`,
				app, c.opID, c.user, c.method, hash, p.ops, seq, p.expect, p.made, string(result))
			if err != nil || tag.RowsAffected() == 0 {
				return err
			}
			inserted = true
			for _, st := range p.stmts {
				tag, err := tx.Exec(ctx, st.sql, st.args...)
				if err != nil {
					return err
				}
				if st.zero != nil && tag.RowsAffected() == 0 {
					return st.zero
				}
			}
			// Each statement writes rows of a snapshot (app_list, app_property, app_item, app_metric). CreateSnapshot compares the version.
			if len(p.stmts) > 0 {
				_, err = tx.Exec(ctx, "update app_application set version = version + 1 where id = $1", app)
			}
			return err
		})
		if err != nil {
			return nil, dbError(err)
		}
		if !inserted {
			continue // Another process inserted the same client_op_id. Step 1 finds its row.
		}
		result, err = s.settle(ctx, app, row)
		if again(err) {
			continue
		}
		return result, err
	}
}

// errConflict: the engine refused the write because of expect. The row is gone, so the plan can run again.
var errConflict = connect.NewError(connect.CodeAborted, errors.New("ほかの書き込みと重なった。もう一度試す"))

// settlePending settles the pending rows of the application in their order (step 2 of change).
func (s *PlanServer) settlePending(ctx context.Context, app string) error {
	rows, _ := s.Pool.Query(ctx, `select client_op_id, user_name, method, request_hash, coalesce(ops, '[]'), coalesce(seq, 0), expect,
		coalesce(made, '[]'), coalesce(result, '{}') from app_operation where app_id = $1 and status = 'pending' order by created_at, client_op_id`, app)
	pending, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (opRow, error) {
		r := opRow{status: "pending"}
		return r, row.Scan(&r.opID, &r.user, &r.method, &r.hash, &r.ops, &r.seq, &r.expect, &r.made, &r.result)
	})
	if err != nil {
		return dbError(err)
	}
	for _, r := range pending {
		// A refusal is the stored result of that operation, not of this one. A conflict deletes the row: its
		// client plans again when it sends the request again.
		if _, err := s.settle(ctx, app, r); err != nil && connect.CodeOf(err) == connect.CodeUnavailable {
			return err
		}
	}
	return nil
}

// settle sends a pending row to the engine (step 5 of change) and flips the row (step 6). It gives the stored
// result, or the stored error of a refusal. If the engine result is not known, the row stays pending and the
// error is Unavailable: the client sends the request again with the same client_op_id. A conflict (expect)
// deletes the row and gives errConflict.
func (s *PlanServer) settle(ctx context.Context, app string, r opRow) (json.RawMessage, error) {
	reply := engineReply{Status: 200}
	if r.method == "CreateApplication" {
		if err := s.Engines.Create(ctx, app); err != nil {
			return nil, err
		}
	}
	if len(r.ops) > 0 {
		var expect *int64
		if r.expect {
			expect = &r.seq
		}
		var err error
		if reply, err = s.Engines.write(ctx, app, r.user, r.opID, r.ops, expect); err != nil {
			return nil, err
		}
	}
	var stored *storedError
	out := reply.outcome()
	switch out {
	case done, conflict:
	case failed:
		stored = &storedError{connect.CodeOf(reply.connectError()), reply.connectError().(*connect.Error).Message()}
	default:
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンの結果が分からない。もう一度試す"))
	}
	// The first statement flips (or deletes) the row. 0 rows: another process settled the row first, and its result
	// is the same, because the engine records the result. A deadlock between two settles runs the transaction again.
	flip := stmt{sql: "update app_operation set status = 'done', ops = null where app_id = $1 and client_op_id = $2 and status = 'pending'", args: []any{app, r.opID}}
	if stored != nil {
		errJSON, _ := json.Marshal(stored)
		flip = stmt{sql: "update app_operation set status = 'failed', error = $3, ops = null where app_id = $1 and client_op_id = $2 and status = 'pending'", args: []any{app, r.opID, string(errJSON)}}
	}
	if out == conflict {
		flip = stmt{sql: "delete from app_operation where app_id = $1 and client_op_id = $2 and status = 'pending'", args: []any{app, r.opID}}
	}
	for try := 0; ; try++ {
		err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
			tag, err := tx.Exec(ctx, flip.sql, flip.args...)
			if err != nil || tag.RowsAffected() == 0 || out == done {
				return err
			}
			for _, st := range compensation(app, r.made) {
				if _, err := tx.Exec(ctx, st.sql, st.args...); err != nil {
					return err
				}
			}
			return nil
		})
		if err == nil {
			break
		}
		if !isDeadlock(err) || try == 2 {
			return nil, dbError(err)
		}
	}
	if out == conflict {
		return nil, errConflict
	}
	if stored != nil {
		return nil, connect.NewError(stored.Code, errors.New(stored.Message))
	}
	return r.result, nil
}

// apiOnly applies a change that only the api tables take, in one transaction with its app_operation row (done).
// A resend with the same client_op_id gives the stored result. f gives the result to store.
func (s *PlanServer) apiOnly(ctx context.Context, app string, req proto.Message, f func(tx pgx.Tx) (any, error)) (json.RawMessage, error) {
	return s.apiOnlyTx(ctx, app, req, pgx.TxOptions{}, f)
}

func (s *PlanServer) apiOnlyTx(ctx context.Context, app string, req proto.Message, opts pgx.TxOptions, f func(tx pgx.Tx) (any, error)) (json.RawMessage, error) {
	c := callerOf(ctx)
	hash := requestHash(req)
	var result json.RawMessage
	err := pgx.BeginTxFunc(ctx, s.Pool, opts, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `insert into app_operation (app_id, client_op_id, user_name, method, request_hash, status)
			values ($1, $2, $3, $4, $5, 'done') on conflict do nothing`, app, c.opID, c.user, c.method, hash)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return errResent
		}
		v, err := f(tx)
		if err != nil {
			return err
		}
		if result, err = json.Marshal(v); err != nil || v == nil {
			result = []byte("{}")
		}
		_, err = tx.Exec(ctx, "update app_operation set result = $3 where app_id = $1 and client_op_id = $2", app, c.opID, string(result))
		return err
	})
	if errors.Is(err, errResent) {
		row, _, err := s.lookupOp(ctx, app, c.opID)
		if err != nil {
			return nil, err
		}
		if err := row.same(c, hash); err != nil {
			return nil, err
		}
		return row.result, nil
	}
	if err != nil {
		return nil, dbError(err)
	}
	return result, nil
}

var errResent = errors.New("resent")

// ackOf changes the result of change or apiOnly into an Ack response.
func ackOf(_ json.RawMessage, err error) (*ack, error) {
	if err != nil {
		return nil, err
	}
	return ok()
}
