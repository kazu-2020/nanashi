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

type ack = connect.Response[nanashiv1.Ack]

// caller is the user and the RPC of a request.
type caller struct {
	user   string
	method string // The RPC name, for example "CreateList".
	opID   string // The client_op_id of a request that changes data. Empty for a read.
}

type callerKey struct{}

func callerOf(ctx context.Context) caller { return ctx.Value(callerKey{}).(caller) }

var constraintMessages = map[string]string{
	"app_application_pkey": "同じ id のアプリケーションがすでにある",
	"app_list_pkey":        "同じ id のリストがすでにある",
	"app_property_pkey":    "同じ id のプロパティがすでにある",
	"app_property_name":    "同じ名前のプロパティがすでにある",
	"app_item_pkey":        "同じ id がすでにある",
	"app_metric_pkey":      "同じ id のメトリックがすでにある",
	"app_comment_pkey":     "同じ id のコメントがすでにある",
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

// checkID gives an InvalidArgument error if s is not a UUID in the canonical form (lower case, with hyphens).
// The engine and the jsonb columns compare ids as strings, so each id has one spelling only.
func checkID(field, s string) error {
	if u, err := uuid.Parse(s); err != nil || u.String() != s {
		return invalid(fmt.Errorf("%s が正規形の UUID ではない: %q", field, s))
	}
	return nil
}

func newID() string { return uuid.Must(uuid.NewV7()).String() }

// Request fields that hold ids: every string field with one of these names, the keys of these map fields, and
// the values of these map fields. checkIDs checks them.
var (
	idFields = map[string]bool{"id": true, "app_id": true, "client_op_id": true, "snapshot_id": true, "metric": true,
		"list": true, "target": true, "member_list": true, "dimensions": true, "rows": true, "columns": true,
		"metrics": true, "members": true, "ids": true, "page_selectors": true, "copy_from": true, "view_id": true, "member": true}
	idKeys   = map[string]bool{"filters": true, "coords": true, "cell": true, "properties": true, "property_columns": true, "dimension_columns": true}
	idValues = map[string]bool{"coords": true, "cell": true}
)

// checkIDs checks each id in a request with checkID. An empty string field is not set, so an optional reference
// stays empty.
func checkIDs(m protoreflect.Message) error {
	var fail error
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		name := string(fd.Name())
		switch {
		case fd.IsMap():
			keys, values := idKeys[name], idValues[name]
			messages := fd.MapValue().Kind() == protoreflect.MessageKind
			v.Map().Range(func(k protoreflect.MapKey, v protoreflect.Value) bool {
				if keys {
					fail = checkID(name, k.String())
				}
				if fail == nil && values {
					fail = checkID(name, v.String())
				}
				if fail == nil && messages {
					fail = checkIDs(v.Message())
				}
				return fail == nil
			})
		case fd.IsList() && fd.Kind() == protoreflect.StringKind && idFields[name]:
			for i, l := 0, v.List(); i < l.Len() && fail == nil; i++ {
				fail = checkID(name, l.Get(i).String())
			}
		case fd.IsList() && fd.Kind() == protoreflect.MessageKind:
			for i, l := 0, v.List(); i < l.Len() && fail == nil; i++ {
				fail = checkIDs(l.Get(i).Message())
			}
		case fd.Kind() == protoreflect.MessageKind:
			fail = checkIDs(v.Message())
		case fd.Kind() == protoreflect.StringKind && idFields[name]:
			fail = checkID(name, v.String())
		}
		return fail == nil
	})
	return fail
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

func detailOf(m proto.Message) string {
	b, _ := protojson.Marshal(m)
	return string(b)
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

// madeRow is a row that the first transaction makes or changes. A refusal of the engine deletes it, or puts the
// old values back (compensation). The compensation changes a value only if it still has the new value, so it
// does not undo a later change.
type madeRow struct {
	Table  string `json:"table"`
	ID     string `json:"id"`
	ListID string `json:"list_id,omitempty"`
	// app_metric only. New is the values that the change wrote. Old is the values before the change (nil: no row).
	Old *metricRow `json:"old,omitempty"`
	New *metricRow `json:"new,omitempty"`
	// app_property_text only: the TEXT values of the property ID, by member. nil: the member has no value.
	OldText map[string]*string `json:"old_text,omitempty"`
	NewText map[string]*string `json:"new_text,omitempty"`
	// app_property_name only: the name of the property ID before and after the change.
	OldName string `json:"old_name,omitempty"`
	NewName string `json:"new_name,omitempty"`
}

// plan is one change to an application: the engine operations, the statements for the api tables, the rows
// that the statements make, and the result for the client.
type plan struct {
	ops    []op
	stmts  []stmt
	made   []madeRow
	seq    int64 // The version of the model that the plan read.
	result any   // The stored result of the operation. nil gives {}.
}

type opRow struct {
	opID, user, method, hash, status string
	ops                              []op
	seq                              int64
	made                             []madeRow
	result                           json.RawMessage
	err                              json.RawMessage
}

type storedError struct {
	Code    connect.Code `json:"code"`
	Message string       `json:"message"`
}

func (r opRow) error() error {
	var e storedError
	json.Unmarshal(r.err, &e)
	return connect.NewError(e.Code, errors.New(e.Message))
}

func (r opRow) same(c caller, hash string) error {
	if r.user != c.user || r.method != c.method || r.hash != hash {
		return invalid(errors.New("同じ client_op_id の別の要求がすでにある"))
	}
	return nil
}

// compensation gives the statements that undo the rows of a refused change.
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
		case "app_property_name":
			out = append(out, stmt{sql: "update app_property set name = $5 where app_id = $1 and list_id = $2 and id = $3 and name = $4",
				args: []any{app, m.ListID, m.ID, m.NewName, m.OldName}})
		case "app_property_text":
			// Each member whose value is still the new value ('null': no value) gets the old value back.
			out = append(out, stmt{sql: `update app_property set text_values = jsonb_strip_nulls(text_values || (
				select coalesce(jsonb_object_agg(n.key, o.value), '{}') from jsonb_each($4::jsonb) n join jsonb_each($5::jsonb) o on o.key = n.key
				where coalesce(text_values -> n.key, 'null'::jsonb) = n.value))
				where app_id = $1 and list_id = $2 and id = $3`, args: []any{app, m.ListID, m.ID, jsonText(m.NewText), jsonText(m.OldText)}})
		}
	}
	return out
}

func jsonText(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

// The tags give an error its Connect code (connectError). An error without a tag is an input error.
var (
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

func execStmt(ctx context.Context, tx pgx.Tx, st stmt) error {
	tag, err := tx.Exec(ctx, st.sql, st.args...)
	if err == nil && st.zero != nil && tag.RowsAffected() == 0 {
		return st.zero
	}
	return err
}

// Migrate makes the api tables (prefix app_) if they are missing.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx, schema)
	return err
}

// Interceptor identifies the user, checks the ids and the application, and records the audit trail.
func (s *PlanServer) Interceptor() connect.UnaryInterceptorFunc {
	return func(next connect.UnaryFunc) connect.UnaryFunc {
		return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
			// The API trusts this header only from this host or from a --trusted-proxy (cmd/nanashi-api).
			user := req.Header().Get("X-Nanashi-User")
			if user == "" {
				return nil, connect.NewError(connect.CodeUnauthenticated, errors.New("X-Nanashi-User がない"))
			}
			method := path.Base(req.Spec().Procedure)
			msg := req.Any().(proto.Message)
			if err := checkIDs(msg.ProtoReflect()); err != nil {
				return nil, err
			}
			c := caller{user: user, method: method}
			appID := ""
			if a, ok := req.Any().(interface{ GetAppId() string }); ok {
				if appID = a.GetAppId(); appID == "" {
					return nil, invalid(errors.New("app_id が要る"))
				}
				var done bool
				if err := s.Pool.QueryRow(ctx, "select exists (select 1 from app_application a where a.id = $1 and "+doneApps+")", appID).Scan(&done); err != nil {
					return nil, dbError(err)
				}
				if !done {
					return nil, connect.NewError(connect.CodeNotFound, errors.New("アプリケーションがない"))
				}
			}
			// Each request that changes data has a client_op_id. checkIDs checked its form.
			if r, ok := req.Any().(interface{ GetClientOpId() string }); ok {
				if c.opID = r.GetClientOpId(); c.opID == "" {
					return nil, invalid(errors.New("client_op_id が要る"))
				}
			}
			res, err := next(context.WithValue(ctx, callerKey{}, c), req)
			// WriteCells has no app_operation row, so the interceptor audits it. The other changes audit themselves, one
			// time for each client_op_id: outbox when the row flips to done, apiOnly in its transaction.
			if err == nil && method == "WriteCells" {
				if _, err := s.Pool.Exec(ctx, "insert into app_audit (app_id, user_name, action, detail) values ($1, $2, $3, $4)",
					appID, user, method, detailOf(msg)); err != nil {
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
	rows, _ := s.Pool.Query(ctx, `select a.id, a.name from app_application a where `+doneApps+` order by a.created_at, a.id`)
	apps, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Application, error) {
		a := &nanashiv1.Application{}
		return a, row.Scan(&a.Id, &a.Name)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.ListApplicationsResponse{Applications: apps}), nil
}

func (s *PlanServer) lookupOp(ctx context.Context, app, opID string) (opRow, bool, error) {
	r := opRow{opID: opID}
	err := s.Pool.QueryRow(ctx, `select user_name, method, request_hash, status, coalesce(ops, '[]'), coalesce(seq, 0),
		coalesce(made, '[]'), coalesce(result, '{}'), coalesce(error, '{}') from app_operation where app_id = $1 and client_op_id = $2`, app, opID).
		Scan(&r.user, &r.method, &r.hash, &r.status, &r.ops, &r.seq, &r.made, &r.result, &r.err)
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
//  5. settle sends the operations to the engine and flips the row to done (with the audit row) or failed (with
//     the compensation) in the second transaction.
func (s *PlanServer) change(ctx context.Context, app string, req proto.Message, planOf func(engineModel, appMeta) (plan, error)) (json.RawMessage, error) {
	return s.outbox(ctx, app, req, func() (plan, error) {
		em, _, err := s.Engines.model(ctx, app)
		if err != nil {
			return plan{}, err
		}
		meta, err := metaIn(ctx, s.Pool, app)
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
		if again(err) {
			continue // The plan read cells of another version than the model (CreateScenario). Plan again.
		}
		if err != nil {
			return nil, err
		}
		result, err := json.Marshal(p.result)
		if err != nil || p.result == nil {
			result = []byte("{}")
		}
		row = opRow{opID: c.opID, user: c.user, method: c.method, hash: hash, status: "pending", ops: p.ops, seq: p.seq, made: p.made, result: result}
		inserted := false
		err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
			tag, err := tx.Exec(ctx, `insert into app_operation (app_id, client_op_id, user_name, method, request_hash, ops, seq, made, result, detail, status)
				values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending') on conflict do nothing`,
				app, c.opID, c.user, c.method, hash, p.ops, p.seq, p.made, string(result), detailOf(req))
			if err != nil || tag.RowsAffected() == 0 {
				return err
			}
			inserted = true
			for _, st := range p.stmts {
				if err := execStmt(ctx, tx, st); err != nil {
					return err
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

// errConflict: the plan read cells of another version than the model. No row exists, so the plan can run again.
var errConflict = connect.NewError(connect.CodeAborted, errors.New("ほかの書き込みと重なった。もう一度試す"))

// settlePending settles the pending rows of the application in their order (step 2 of change).
func (s *PlanServer) settlePending(ctx context.Context, app string) error {
	rows, _ := s.Pool.Query(ctx, `select client_op_id, user_name, method, request_hash, coalesce(ops, '[]'), coalesce(seq, 0),
		coalesce(made, '[]'), coalesce(result, '{}') from app_operation where app_id = $1 and status = 'pending' order by created_at, client_op_id`, app)
	pending, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (opRow, error) {
		r := opRow{status: "pending"}
		return r, row.Scan(&r.opID, &r.user, &r.method, &r.hash, &r.ops, &r.seq, &r.made, &r.result)
	})
	if err != nil {
		return dbError(err)
	}
	for _, r := range pending {
		// A refusal is the stored result of that operation, not of this one. A row whose result stays unknown does
		// not block this operation: the engine orders the writes, so a later settle of the row is a later write.
		// If the engine is down, the engine read or write of this operation fails on its own.
		if _, err := s.settle(ctx, app, r); err != nil && ctx.Err() != nil {
			return err
		}
	}
	return nil
}

// settle sends a pending row to the engine and flips the row (step 5 of change). It gives the stored
// result, or the stored error of a refusal. If the engine result is not known, the row stays pending and the
// error is Unavailable: the client sends the request again with the same client_op_id. The flip to done writes
// the audit row, so an operation has one audit row, also when another request settles it.
func (s *PlanServer) settle(ctx context.Context, app string, r opRow) (json.RawMessage, error) {
	reply := engineReply{Status: 200}
	if r.method == "CreateApplication" {
		if err := s.Engines.Create(ctx, app); err != nil {
			return nil, err
		}
	}
	if len(r.ops) > 0 {
		var err error
		if reply, err = s.Engines.write(ctx, app, r.user, r.opID, r.ops); err != nil {
			return nil, err
		}
	}
	var stored *storedError
	out := reply.outcome()
	switch out {
	case done:
	case failed:
		ce := reply.connectError().(*connect.Error)
		stored = &storedError{ce.Code(), ce.Message()}
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
	for try := 0; ; try++ {
		err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
			tag, err := tx.Exec(ctx, flip.sql, flip.args...)
			if err != nil || tag.RowsAffected() == 0 {
				return err
			}
			if out == done {
				_, err = tx.Exec(ctx, `insert into app_audit (app_id, user_name, action, detail)
					select app_id, user_name, method, coalesce(detail, '') from app_operation where app_id = $1 and client_op_id = $2`, app, r.opID)
				return err
			}
			for _, st := range compensation(app, r.made) {
				if err := execStmt(ctx, tx, st); err != nil {
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
	if stored != nil {
		return nil, connect.NewError(stored.Code, errors.New(stored.Message))
	}
	return r.result, nil
}

// apiOnly applies a change that only the api tables take, in one transaction with its app_operation row (done)
// and its audit row. A resend with the same client_op_id gives the stored result. f gives the result to store.
// opts sets the isolation level of the transaction.
func (s *PlanServer) apiOnly(ctx context.Context, app string, req proto.Message, opts pgx.TxOptions, f func(tx pgx.Tx) (any, error)) (json.RawMessage, error) {
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
		if _, err := tx.Exec(ctx, "insert into app_audit (app_id, user_name, action, detail) values ($1, $2, $3, $4)", app, c.user, c.method, detailOf(req)); err != nil {
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

func ackOf(_ json.RawMessage, err error) (*ack, error) {
	if err != nil {
		return nil, err
	}
	return ok()
}

func createdAt(result json.RawMessage) int64 {
	var r struct {
		CreatedAt int64 `json:"created_at"`
	}
	json.Unmarshal(result, &r)
	return r.CreatedAt
}
